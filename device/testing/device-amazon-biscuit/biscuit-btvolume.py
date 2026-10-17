#!/usr/bin/env python3
"""One volume for a phone playing to the Echo over Bluetooth.

The phone's volume used to land on the codec. bluealsa-aplay ran with
--mixer-control=PCM, so every move of the phone's slider was written to the
codec's PCM Playback Volume - a control biscuit-audio holds at 127, unity,
because the volume law lives in biscuit-dsp. Two things followed:

  * the phone and the Echo each had a volume, in series, and neither knew about
    the other: the buttons did not move the phone's slider, and the phone did
    not move the Echo's level or its ring;
  * the control goes to 175, 24 dB above unity, so the phone could drive the
    Echo far past its loudest setting.

Now bluealsa-aplay runs with --volume=none and touches no mixer; the A2DP
volume is only reported, as the PCM's Volume property (0-127). This bridges
that property and biscuit-audio's device volume, both ways:

  phone -> Echo   a Volume change becomes `volume N` on biscuit-audio's control
                  FIFO, so it is the device volume, with the ring and the tick.
  Echo -> phone   a device volume change (buttons, Home Assistant, Music
                  Assistant) is set as the PCM's Volume, which bluez-alsa sends
                  to the phone as AVRCP absolute volume, so its slider follows.

When a phone connects it is given the Echo's level, rather than the Echo taking
the phone's: a phone left at full volume would otherwise start the Echo at its
loudest.

Each direction ignores the echo of its own last change for SETTLE_S, inside a
tolerance: the Echo has thirty steps, so a level from the phone comes back
rounded by up to two points, and bluez-alsa repeats a Volume it was given.
Without that the two sides would chase each other's rounding.
"""
import os
import shutil
import subprocess
import sys
import threading
import time

AUDIO_CONTROL = "/run/biscuit-audio/control"
AUDIO_VOLUME = "/run/biscuit-audio/volume"
BT_MAX = 127            # A2DP / AVRCP absolute volume range
SETTLE_S = 2.0
PCT_TOLERANCE = 2       # 30 steps are 3.3 points apart
BT_TOLERANCE = 2
POLL_S = 0.2
RESTART_S = 5.0


def log(msg):
    sys.stdout.write("biscuit-btvolume: %s\n" % msg)
    sys.stdout.flush()


def bluealsactl():
    return (shutil.which("bluealsactl")
            or ("/usr/local/bin/bluealsactl"
                if os.access("/usr/local/bin/bluealsactl", os.X_OK) else None))


def bt_to_pct(value):
    return max(0, min(100, int(round(value * 100.0 / BT_MAX))))


def pct_to_bt(pct):
    return max(0, min(BT_MAX, int(round(pct * BT_MAX / 100.0))))


def read_device_pct():
    try:
        with open(AUDIO_VOLUME) as handle:
            return max(0, min(100, int(handle.read().strip())))
    except (OSError, ValueError):
        return None


def request_device_pct(pct):
    """Ask biscuit-audio for a level. Non-blocking: it may not be running."""
    try:
        fd = os.open(AUDIO_CONTROL, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, ("volume %d\n" % pct).encode())
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def is_a2dp_sink(path):
    # The Echo is the sink; the phone's stream is .../a2dpsnk/source.
    return "/a2dpsnk/" in path


class Bridge(object):
    def __init__(self, ctl):
        self.ctl = ctl
        self.lock = threading.Lock()
        self.pcms = set()
        # Last level each side was told by us, and when.
        self.sent_bt = None      # (value, time) set on the phone
        self.sent_pct = None     # (pct, time) requested from biscuit-audio
        self.device_pct = read_device_pct()

    # -- Echo -> phone ---------------------------------------------------
    def set_phone(self, path, pct):
        value = pct_to_bt(pct)
        with self.lock:
            self.sent_bt = (value, time.monotonic())
        try:
            result = subprocess.run([self.ctl, "volume", path, str(value), str(value)],
                                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as err:
            # A stalled bluealsad must cost this one update, not the thread
            # that calls this: watch_device would die with it, and the Echo's
            # level would never reach the phone again.
            log("could not set %s to %d: %s" % (path, value, err))
            return
        if result.returncode != 0:
            log("could not set %s to %d: %s" % (path, value, result.stderr.strip()))

    def push_to_phones(self, pct):
        with self.lock:
            paths = list(self.pcms)
        for path in paths:
            self.set_phone(path, pct)

    def device_changed(self, pct):
        """biscuit-audio published a new level."""
        with self.lock:
            sent = self.sent_pct
            explained = (sent is not None
                         and time.monotonic() - sent[1] < SETTLE_S
                         and abs(pct - sent[0]) <= PCT_TOLERANCE)
        if explained:
            return       # the phone asked for this; do not send it back
        self.push_to_phones(pct)

    # -- phone -> Echo ---------------------------------------------------
    def phone_changed(self, path, value):
        with self.lock:
            sent = self.sent_bt
            echo = (sent is not None
                    and time.monotonic() - sent[1] < SETTLE_S
                    and abs(value - sent[0]) <= BT_TOLERANCE)
        if echo:
            return       # our own change coming back from bluez-alsa
        pct = bt_to_pct(value)
        with self.lock:
            self.sent_pct = (pct, time.monotonic())
        if not request_device_pct(pct):
            log("biscuit-audio is not listening; phone volume %d not applied" % value)

    def pcm_added(self, path):
        with self.lock:
            new = path not in self.pcms
            self.pcms.add(path)
        if new:
            pct = read_device_pct()
            if pct is not None:
                log("%s connected; giving it the Echo's %d%%" % (path, pct))
                self.set_phone(path, pct)

    def pcm_removed(self, path):
        with self.lock:
            self.pcms.discard(path)

    # -- sources ---------------------------------------------------------
    def scan(self):
        result = subprocess.run([self.ctl, "list-pcms"], capture_output=True,
                                text=True, timeout=5)
        for line in result.stdout.split():
            if is_a2dp_sink(line):
                self.pcm_added(line)

    def watch_device(self):
        last_stat = None
        while True:
            time.sleep(POLL_S)
            try:
                st = os.stat(AUDIO_VOLUME)
            except OSError:
                continue
            key = (st.st_ino, st.st_mtime_ns)
            if key == last_stat:
                continue
            last_stat = key
            pct = read_device_pct()
            if pct is None or pct == self.device_pct:
                continue
            self.device_pct = pct
            try:
                self.device_changed(pct)
            except Exception as err:  # noqa: BLE001 - this thread must outlive any one update
                log("could not pass volume %d to the phone: %s" % (pct, err))

    def watch_bluealsa(self):
        """Follow bluealsactl monitor until it exits; the caller restarts it."""
        proc = subprocess.Popen([self.ctl, "monitor", "--properties=Volume"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True)
        try:
            # Anything connected before the monitor started is only found by
            # asking, and again after every restart of bluealsa.
            self.scan()
            for line in proc.stdout:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "PCMAdded" and is_a2dp_sink(parts[1]):
                    self.pcm_added(parts[1])
                elif len(parts) >= 2 and parts[0] == "PCMRemoved":
                    self.pcm_removed(parts[1])
                elif (len(parts) >= 4 and parts[0] == "PropertyChanged"
                      and parts[2] == "Volume" and is_a2dp_sink(parts[1])):
                    try:
                        value = max(int(v) for v in parts[3:])
                    except ValueError:
                        continue
                    self.pcm_added(parts[1])
                    self.phone_changed(parts[1], value)
                elif parts[:1] == ["ServiceStopped"]:
                    with self.lock:
                        self.pcms.clear()
        finally:
            proc.kill()
            proc.wait()


def main():
    ctl = None
    while ctl is None:
        ctl = bluealsactl()
        if ctl is None:
            time.sleep(RESTART_S)
    bridge = Bridge(ctl)
    watcher = threading.Thread(target=bridge.watch_device)
    watcher.daemon = True
    watcher.start()
    log("bridging Bluetooth volume with %s via %s" % (AUDIO_VOLUME, ctl))
    while True:
        try:
            bridge.watch_bluealsa()
        except Exception as err:  # noqa: BLE001 - keep bridging
            log("monitor failed: %s" % err)
        time.sleep(RESTART_S)


if __name__ == "__main__":
    sys.exit(main())
