# SPDX-License-Identifier: Apache-2.0
#
# NEW FILE, not present upstream. Written by liamtw22 and contributors,
# 2026, for this port's fork of OHF-Voice/linux-voice-assistant
# (based on commit b0c53c41c11e),
#   https://github.com/OHF-Voice/linux-voice-assistant
# and licensed under the Apache License 2.0 like the rest of that fork
# (LICENSES/Apache-2.0.txt).
# Purpose: read-only qualification observations used by the test harness.
"""Read-only qualification observations; no native player state calls or audio I/O.

Mic progress is one immutable tuple assignment and one monotonic clock read.
Snapshots never wait for player/activity locks. Instrumentation errors become
unknown and must not change the result or exception of the original operation.
"""
from functools import wraps
import os
from pathlib import Path
import threading
import time

_identity = (None, None, None)
try:
    _pid = os.getpid()
    _stat = Path('/proc/self/stat').read_text()
    _identity = (Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                 _pid, int(_stat[_stat.rfind(')')+1:].split()[19]))
except Exception:
    pass
_activity_lock = threading.Lock()
_activity_depth = 0
_pending_timers = 0
_activity_epoch = 0
_broken = False


def mic_progress(state, ready):
    """Only called at completed/skipped/failed audio-iteration boundaries."""
    try:
        previous = getattr(state, '_qualification_mic', (0, None, False))
        state._qualification_mic = (previous[0]+1, time.monotonic(), bool(ready))
    except Exception:
        # A stale or missing tuple cannot pass the freshness gate.
        pass


def _change_activity(delta=0, pending=0):
    global _activity_depth, _pending_timers, _activity_epoch, _broken
    try:
        with _activity_lock:
            _activity_depth += delta
            _pending_timers += pending
            _activity_epoch += 1
            if _activity_depth < 0 or _pending_timers < 0:
                _broken = True
    except Exception:
        _broken = True


def activity(function):
    """Preserve callback/play behavior while exposing entry-to-exit demand."""
    @wraps(function)
    def observed(*args, **kwargs):
        _change_activity(1)
        try:
            return function(*args, **kwargs)
        finally:
            _change_activity(-1)
    return observed


def pending_timer(interval, function):
    """Track the existing unretained continuation Timer without changing delay.

    Current source never cancels this Timer. Any future cancellation leaves a
    conservative pending count until restart; it can never create false idle.
    """
    _change_activity(pending=1)
    @wraps(function)
    def fired():
        _change_activity(1)
        try:
            return function()
        finally:
            _change_activity(-1, pending=-1)
    return threading.Timer(interval, fired)


def satellite_created(satellite):
    try:
        satellite._qualification_ha_configured = False
        state = satellite.state
        if not hasattr(state, '_qualification_observed_timers'):
            state._qualification_observed_timers = frozenset()
            state._qualification_timers_overflow = False
    except Exception:
        pass


def configured(satellite, value):
    try:
        satellite._qualification_ha_configured = bool(value)
    except Exception:
        pass


def timer_event(satellite, event_type, message):
    """Track IDs internally, including UPDATE/paused timers; never export IDs."""
    try:
        state = satellite.state
        previous = getattr(state, '_qualification_observed_timers', None)
        if previous is None:
            return
        event = event_type.name
        timer_id = message.timer_id
        if not isinstance(timer_id, str) or not timer_id or len(timer_id) > 256:
            state._qualification_timers_overflow = True
            return
        if event in ('VOICE_ASSISTANT_TIMER_STARTED', 'VOICE_ASSISTANT_TIMER_UPDATED'):
            if len(previous) >= 64 and timer_id not in previous:
                state._qualification_timers_overflow = True
            else:
                state._qualification_observed_timers = previous | {timer_id}
        elif event in ('VOICE_ASSISTANT_TIMER_CANCELLED', 'VOICE_ASSISTANT_TIMER_FINISHED'):
            state._qualification_observed_timers = previous - {timer_id}
        else:
            state._qualification_timers_overflow = True
    except Exception:
        try:
            satellite.state._qualification_timers_overflow = True
        except Exception:
            pass


def _boolean(value):
    return value if type(value) is bool else None


def _any(values):
    values = list(values)
    if any(value is True for value in values):
        return True
    return False if values and all(value is False for value in values) else None


def _player(player):
    result = {'active': None, 'queued': None, 'cached_state': None}
    lock = None
    held = False
    try:
        native = player._player
        lock = native._state_lock
        held = lock.acquire(blocking=False)
        if not held:
            return result
        name = native._state.name
        result['cached_state'] = name if name in ('IDLE', 'LOADING', 'PLAYING', 'PAUSED', 'STOPPING', 'ERROR') else None
        if name == 'IDLE':
            result['active'] = False
        elif name in ('LOADING', 'PLAYING', 'PAUSED', 'STOPPING'):
            result['active'] = True
        # ERROR remains unknown; an error log does not prove native silence.
        playlist = player._playlist
        if isinstance(playlist, list):
            result['queued'] = bool(playlist or player._done_callback is not None or native._done_callback is not None)
        return result
    except Exception:
        return {'active': None, 'queued': None, 'cached_state': None}
    finally:
        if held:
            lock.release()


def _activity_snapshot():
    if not _activity_lock.acquire(blocking=False):
        return None
    try:
        return (_activity_depth, _pending_timers, _activity_epoch, _broken)
    finally:
        _activity_lock.release()


def snapshot(state):
    result = {'boot_id': _identity[0], 'pid': _identity[1], 'start_ticks': _identity[2],
        'observed_mono': time.monotonic(), 'ha_connected': None, 'ha_configured': None,
        'wake_ready': None, 'mic_last_block_mono': None, 'mic_blocks': None,
        'pipeline_active': None, 'streaming_audio': None, 'timer_active': None,
        'timer_ringing': None, 'tts_active': None, 'music_active': None,
        'queued_playback': None, 'remote_timer_inventory_known': False,
        'observed_timer_count': None, 'callbacks_active': None, 'pending_timer_callbacks': None}
    try:
        result['ha_connected'] = _boolean(state.connected)
        current = state.satellite
        result['ha_configured'] = (False if result['ha_connected'] is False else
            _boolean(getattr(current, '_qualification_ha_configured', None)))
        count, stamp, ready = getattr(state, '_qualification_mic', (None, None, None))
        result.update(mic_blocks=count, mic_last_block_mono=stamp, wake_ready=_boolean(ready))
        satellites = list(state.connections)
        if current is not None and all(item is not current for item in satellites):
            satellites.append(current)
        for output, attribute in (('pipeline_active', '_pipeline_active'),
                                  ('streaming_audio', '_is_streaming_audio'),
                                  ('timer_ringing', '_timer_finished')):
            result[output] = _any(_boolean(getattr(item, attribute, None)) for item in satellites)
        observed = getattr(state, '_qualification_observed_timers', None)
        if isinstance(observed, frozenset):
            result['observed_timer_count'] = len(observed)
            if observed or getattr(state, '_qualification_timers_overflow', False):
                result['timer_active'] = True
        # No HA timer inventory barrier exists in the observed protocol. Empty
        # local observations MUST remain unknown, including after reconnect.
        before = _activity_snapshot()
        tts, music = _player(state.tts_player), _player(state.music_player)
        after = _activity_snapshot()
        result.update(tts_active=tts['active'], music_active=music['active'])
        if before is not None and before == after and not before[3]:
            result['callbacks_active'] = before[0] > 0
            result['pending_timer_callbacks'] = before[1]
            result['queued_playback'] = _any((tts['queued'], music['queued'], before[0] > 0, before[1] > 0))
        elif (before and (before[0] > 0 or before[1] > 0)) or (after and (after[0] > 0 or after[1] > 0)):
            result['queued_playback'] = True
    except Exception:
        # Fields already backed by an independent read remain usable; missing
        # evidence remains null, and the idle consumer must require every field.
        pass
    return result


# ---------------------------------------------------------------------------
# The 64 ms receipt-to-result gate
# ---------------------------------------------------------------------------
#
# The rule: the clock starts when the consumer RECEIVES a complete block and
# stops when it produces a result or dispatch. It is a consumer-responsiveness
# gate and it lives here, inside the voice assistant.
#
# This was previously argued closed without instrumenting it, from the hub's
# counters: 3.8 million consecutive blocks offered and written with no drops,
# no restarts and no dropped clients, which does rule out a SUSTAINED overrun,
# because the consumer would fall behind and the hub would drop it. What it
# cannot see is a rare single-block spike, which the 128 ms client queue
# absorbs in silence. That is the gap this closes, and it is why the argument
# from delivery counters was not enough: delivery is not result latency.
#
# What is measured here, and what is not:
#
#   measured   receipt -> result, exactly, per block
#   measured   inter-arrival, which is the producer cadence as the consumer
#              sees it, and the thing that reveals a stalled producer
#   derived    a FLOOR on physical sample age at dispatch: the oldest sample in
#              a block is one block-duration older than its arrival, so the age
#              is at least block_ms + latency_ms
#   NOT here   capture -> receipt. That is transport, it belongs to the hub's
#              own gate, and the hub already counts it as drain_max_ms. Adding
#              a guess for it here would make one number out of two gates the
#              rule deliberately keeps distinct.
#
# Startup is counted apart from steady state. The first blocks after a start
# load models, fault in pages and touch cold caches; folding them into the same
# maximum would report a miss that says nothing about the running device.
#
# The number 64 is not arbitrary, and no rationale for it was recorded anywhere
# in this tree. It is one block: the assistant reads 1024 samples at 16 kHz,
# which is 64.0 ms exactly. The gate is therefore "finish a block before the
# next one arrives" - the consumer must not fall behind its own producer.
GATE_MS = 64.0
CADENCE_MS = 64.0         # 1024 samples at 16 kHz; replaced by the real value
WARMUP_BLOCKS = 250       # ~2 s at 8 ms
_TIMING_PATH = '/run/biscuit-mic/lva-timing.json'
_PUBLISH_EVERY_S = 10.0
_BUCKETS_MS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0)

_timing = {
    'blocks': 0, 'warmup_blocks': 0,
    'max_ms': 0.0, 'sum_ms': 0.0,
    'misses': 0, 'startup_misses': 0, 'dispatch_misses': 0,
    'over_cadence': 0,
    'gap_max_ms': 0.0, 'gap_over_2x': 0,
    'publish_max_ms': 0.0, 'publishes': 0,
    'hist': [0] * (len(_BUCKETS_MS) + 1),
}
_timing_last_arrival = None
_timing_last_publish = 0.0
_timing_started = None


def block_received(block_ms=None):
    """Stamp the arrival of one COMPLETE block. Returns a token, or None.

    Call this after the read returns, never before it: the read blocks until
    the producer has a block ready, so a stamp taken ahead of it measures the
    wait for the producer instead of the response of the consumer.
    """
    global _timing_last_arrival, _timing_started, CADENCE_MS
    try:
        if block_ms:
            CADENCE_MS = float(block_ms)
        now = time.monotonic()
        if _timing_started is None:
            _timing_started = now
        previous, _timing_last_arrival = _timing_last_arrival, now
        if previous is not None:
            gap = (now - previous) * 1000.0
            if gap > _timing['gap_max_ms']:
                _timing['gap_max_ms'] = gap
            # Half a block late is the producer slipping; a full block late
            # would already have shown up in the hub's own counters.
            # Counted rather than maximised: one long gap after a restart says
            # less than how often it happens.
            if gap > CADENCE_MS * 1.5:
                _timing['gap_over_2x'] += 1
        return now
    except Exception:
        return None


def block_done(token, dispatched=False):
    """Close the gate for one block. Never raises, never changes the result."""
    global _timing_last_publish
    try:
        if token is None:
            return
        latency = (time.monotonic() - token) * 1000.0
        warm = _timing['blocks'] >= WARMUP_BLOCKS
        _timing['blocks'] += 1
        if not warm:
            _timing['warmup_blocks'] += 1
        index = len(_BUCKETS_MS)
        for i, edge in enumerate(_BUCKETS_MS):
            if latency <= edge:
                index = i
                break
        _timing['hist'][index] += 1
        if latency > CADENCE_MS:
            _timing['over_cadence'] += 1
        if latency > GATE_MS:
            if warm:
                _timing['misses'] += 1
                # A block that also dispatched did more work by definition, so
                # keep those apart: a miss on a wake-word turn is a different
                # claim from a miss on an idle block.
                if dispatched:
                    _timing['dispatch_misses'] += 1
            else:
                _timing['startup_misses'] += 1
        if warm:
            _timing['sum_ms'] += latency
            if latency > _timing['max_ms']:
                _timing['max_ms'] = latency
        now = time.monotonic()
        if now - _timing_last_publish >= _PUBLISH_EVERY_S:
            _timing_last_publish = now
            # Timed, because this instrument writes a file from the audio
            # thread and an instrument that causes the stall it measures is
            # worse than no instrument. It happens AFTER the latency above, so
            # it cannot inflate this block - but it can delay the next read,
            # which would land as a producer gap. Reporting its own cost is
            # what tells the two apart instead of leaving it to argument.
            _publish_started = time.monotonic()
            _publish_timing()
            _publish_ms = (time.monotonic() - _publish_started) * 1000.0
            _timing['publishes'] += 1
            if _publish_ms > _timing['publish_max_ms']:
                _timing['publish_max_ms'] = _publish_ms
    except Exception:
        pass


def _publish_timing():
    """Atomically write the report. One small write per ~1250 blocks."""
    try:
        import json
        warm = _timing['blocks'] - _timing['warmup_blocks']
        doc = {
            'gate_ms': GATE_MS,
            'cadence_ms': CADENCE_MS,
            'blocks': _timing['blocks'],
            'steady_blocks': warm,
            'warmup_blocks': _timing['warmup_blocks'],
            'max_ms': round(_timing['max_ms'], 3),
            'mean_ms': round(_timing['sum_ms'] / warm, 3) if warm > 0 else None,
            'misses': _timing['misses'],
            'startup_misses': _timing['startup_misses'],
            'dispatch_misses': _timing['dispatch_misses'],
            'over_cadence': _timing['over_cadence'],
            'gap_max_ms': round(_timing['gap_max_ms'], 3),
            'gap_over_1_5x': _timing['gap_over_2x'],
            'publish_max_ms': round(_timing['publish_max_ms'], 3),
            'publishes': _timing['publishes'],
            'realtime': _timing.get('realtime'),
            'histogram_ms_edges': list(_BUCKETS_MS),
            'histogram': list(_timing['hist']),
            'sample_age_floor_ms': round(_timing['max_ms'] + CADENCE_MS, 3),
        'block_ms': CADENCE_MS,
            'uptime_s': round(time.monotonic() - _timing_started, 1)
                        if _timing_started else None,
            'note': ('receipt-to-result only; capture-to-receipt is the hub gate '
                     'and is counted there as drain_max_ms'),
        }
        directory = os.path.dirname(_TIMING_PATH)
        if not os.path.isdir(directory):
            return
        temporary = _TIMING_PATH + '.tmp'
        with open(temporary, 'w') as handle:
            json.dump(doc, handle)
        os.replace(temporary, _TIMING_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Real-time scheduling for the consumer thread
# ---------------------------------------------------------------------------
#
# Everything ahead of this thread in the audio path already runs SCHED_FIFO -
# the capture reader at 90, the interrupt at 92, the Fire OS 6 DSP at 45 - and
# the consumer that has to answer within one block was left at SCHED_OTHER
# priority 0, competing with every other process on the device. Measured with
# the gate instrument: one block in 11,147 took 148 ms, with the producer gap
# peaking at the same value, which is a deschedule of the whole thread rather
# than slow work. The instrument ruled itself out at 1.9 ms per publish.
#
# Deliberately BELOW the DSP at 45 and far below the reader at 90: this thread
# must never be able to starve the things that feed it. Above ordinary work, so
# a busy page or a package install cannot push it off a core.
#
# Failure is not fatal and not even a warning at startup: a kernel without
# CONFIG_RT_GROUP_SCHED headroom, or a process without CAP_SYS_NICE, should run
# the assistant exactly as before rather than refuse to start.
REALTIME_PRIORITY = 10


# Raw syscalls, because musl stubs both of the obvious routes. Python's
# os.sched_setscheduler returns ENOSYS ("Function not implemented") - musl
# declines to implement the SCHED_* family because POSIX defines it per process
# while Linux applies it per thread - and os.gettid is not exposed at all. The
# rest of this project shells out to chrt for the same reason; here the thread
# has to set ITSELF, and chrt needs a tid that musl will not hand over.
#
# aarch64 numbers. This package is built for one board, and a wrong number on
# some other architecture would silently reschedule something else, so the
# platform is checked rather than assumed.
_NR_GETTID_AARCH64 = 178
_NR_SCHED_SETSCHEDULER_AARCH64 = 119
_SCHED_FIFO = 1


def claim_realtime(priority=REALTIME_PRIORITY):
    """Put the CALLING THREAD on SCHED_FIFO. Returns True if it took.

    The answer is recorded in the timing report, so a reader can tell a clean
    measurement from one taken at ordinary priority instead of assuming.
    """
    result = _claim_realtime(priority)
    try:
        _timing['realtime'] = bool(result)
    except Exception:
        pass
    return result


def _claim_realtime(priority):
    try:
        import ctypes
        import platform
        if platform.machine() not in ('aarch64', 'arm64'):
            return False
        libc = ctypes.CDLL(None, use_errno=True)

        class _Param(ctypes.Structure):
            _fields_ = [('sched_priority', ctypes.c_int)]

        tid = libc.syscall(_NR_GETTID_AARCH64)
        if tid <= 0:
            return False
        param = _Param(int(priority))
        rc = libc.syscall(_NR_SCHED_SETSCHEDULER_AARCH64, tid, _SCHED_FIFO,
                          ctypes.byref(param))
        return rc == 0
    except Exception:
        return False
