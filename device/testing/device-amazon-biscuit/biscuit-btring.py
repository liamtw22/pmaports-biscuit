#!/usr/bin/env python3
"""Show the Bluetooth ring animations when a device connects.

Stock shipped btconnect and btdiscconnect - note its spelling of the second - on
layer 3, and our own priority table keeps them there. This plays them on the
BlueZ events. The two files are byte-identical on this build, so the two events
currently look the same; they are still played by their own names in case that
ever stops being true.

Why a separate service rather than part of biscuit-audio: this one needs D-Bus
and bluetoothd, and the mute button and jack must keep working when Bluetooth
is not running.

Why gdbus rather than a D-Bus binding: there is no python3-dbus or pydbus on
this rootfs, but gdbus is present and its monitor output is line-oriented and
trivially parseable:

    /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF: \
        org.freedesktop.DBus.Properties.PropertiesChanged \
        ('org.bluez.Device1', {'Connected': <true>}, @as [])

Only Device1 is matched. Adapter1 emits Connectable and Discoverable changes
that look similar and must not trigger anything.
"""
import os
import re
import signal
import subprocess
import sys
import time

RING_FIFO = "/run/biscuit-ring/control"

# Stock's names, and the defaults. Both are user-selectable from the settings
# page, which resolves every activity into /opt/persist/led-anim.env. Read at
# play time rather than at import, so a change applies without restarting this
# service - and falling back to our own act_ animation whenever that file is
# missing,
# unreadable or does not mention this activity.
CONNECT_ANIMATION = "act_bt_connected"
DISCONNECT_ANIMATION = "act_bt_disconnected"
ANIM_ENV = "/opt/persist/led-anim.env"


def chosen(activity, default):
    """LED_<activity> from the resolved env file, or `default`."""
    try:
        with open(ANIM_ENV) as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == "LED_" + activity:
                    value = value.strip().strip("'").strip('"')
                    if value:
                        return value
    except OSError:
        pass
    return default

# Stock's two are one-shot: 46 frames, no loop, so they retire themselves and the
# ring returns to whatever was underneath. A user-selected effect loops, so every
# play is bounded by SHOW_S - long enough that stock's never hits it.
SHOW_S = 3
GDBUS = ["gdbus", "monitor", "--system", "--dest", "org.bluez"]

# Device1 only. Adapter1 property changes use the same signal name.
EVENT_RE = re.compile(
    r"(?P<path>/org/bluez/\S*dev_[0-9A-Fa-f_]+):.*"
    r"PropertiesChanged\s*\(\s*'org\.bluez\.Device1'\s*,\s*"
    r"\{(?P<body>.*?)\}")
CONNECTED_RE = re.compile(r"'Connected':\s*<(true|false)>")

RESTART_DELAY_S = 5.0

_running = True


def log(msg):
    sys.stdout.write("biscuit-btring: %s\n" % msg)
    sys.stdout.flush()


def ring(command):
    """One line to biscuit-ring, non-blocking; ignored if it is not running."""
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


def stop(signum, frame):
    global _running
    _running = False


def handle(line):
    m = EVENT_RE.search(line)
    if not m:
        return
    c = CONNECTED_RE.search(m.group("body"))
    if not c:
        return
    connected = c.group(1) == "true"
    addr = m.group("path").rsplit("dev_", 1)[-1].replace("_", ":")
    log("%s %s" % (addr, "connected" if connected else "disconnected"))
    anim = (chosen("bt_connected", CONNECT_ANIMATION) if connected
            else chosen("bt_disconnected", DISCONNECT_ANIMATION))
    # Bounded on purpose. Stock's two retire themselves after 46 frames, but a
    # user-chosen generated effect loops and would otherwise never stop.
    ring("play %s %d" % (anim, SHOW_S))

    # The matching earcon. Spawned rather than played inline: this runs on the
    # D-Bus watch loop, and blocking it on audio would delay the next
    # connect/disconnect event.
    try:
        subprocess.Popen(
            ["/usr/bin/biscuit-earcon",
             "bt_connected" if connected else "bt_disconnected"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError as err:
        log("earcon did not play: %s" % err)


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while _running:
        try:
            proc = subprocess.Popen(GDBUS, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True,
                                    bufsize=1)
        except OSError as err:
            log("cannot start gdbus: %s" % err)
            return 1

        log("watching org.bluez for Device1 Connected changes")
        try:
            for line in proc.stdout:
                if not _running:
                    break
                handle(line)
        except (OSError, ValueError):
            pass

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        if not _running:
            break
        # bluetoothd restarting takes the bus name with it and gdbus exits.
        # Reconnect rather than dying, so a Bluetooth restart does not
        # silently leave the ring without events until the next reboot.
        log("gdbus exited; retrying in %.0fs" % RESTART_DELAY_S)
        for _ in range(int(RESTART_DELAY_S * 10)):
            if not _running:
                break
            time.sleep(0.1)

    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
