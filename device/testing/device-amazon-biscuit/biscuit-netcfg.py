#!/usr/bin/env python3
"""Network, Bluetooth and account backends for the :8080 settings page.

WHY THIS IS A SEPARATE MODULE
-----------------------------
biscuit-settings.py already loads biscuit-va-leds.py as `agent` to avoid keeping
a second copy of "what the audio and ring settings are". That module is the
voice-assistant agent, though, and Wi-Fi, Bluetooth and the user account have
nothing to do with it - they are not exposed to Home Assistant and never will
be, because a settings page that can only be reached over the network is a poor
place to reconfigure the network.

So the three subsystems the settings page owns exclusively live here, loaded the
same way. Standard library only, matching the rest of the device's Python.

WHAT EACH SUBSYSTEM TALKS TO
----------------------------
Wi-Fi        wpa_supplicant's control socket, directly. See WpaCtrl below for
             why this is a raw socket rather than shelling out to wpa_cli.
Bluetooth    bluetoothctl, which is non-interactive for single commands in
             BlueZ 5.5x+ (the image ships 5.87).
Account      /etc/passwd and chpasswd, plus the persist copy of /etc/hostname.

PERSISTENCE IS ALREADY SOLVED
-----------------------------
biscuit-persist bind-mounts /opt/persist/wpa_supplicant onto /etc/wpa_supplicant
and /opt/persist/bluetooth onto /var/lib/bluetooth before `local` and
`bluetooth` start. So a wpa_supplicant SAVE_CONFIG and a BlueZ pairing both land
on the persist partition with no extra work here, and survive a flash. The
hostname is the one exception - it is a plain file on the rootfs - so
set_device_name writes both copies, exactly as biscuit-persist's own start
function does.
"""

import contextlib
import glob
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid

WPA_IFACE = "wlan0"
WPA_CTRL_DIR = "/var/run/wpa_supplicant"
WPA_CONF = "/etc/wpa_supplicant/wpa_supplicant.conf"
# Where this process binds its end of the control socket. Root only;
# see WpaCtrl for why it is not /tmp.
WPA_CLIENT_DIR = "/run/biscuit-netcfg"
HOSTNAME_FILE = "/etc/hostname"
PERSIST_HOSTNAME = "/opt/persist/etc/hostname"
PROVISIONED = "/opt/persist/provisioned"

# The account postmarketOS ships and biscuit-setup.sh retires. It is locked
# rather than deleted, so it is still in /etc/passwd and must be filtered out of
# anything that shows the user "your account".
STOCK_ACCOUNT = "user"

# The assistant, Sendspin, Avahi and BlueZ all publish the device name. Renaming
# the device has to reach all of them or the name shown in Home Assistant, in
# Music Assistant, on the network and in a phone's Bluetooth list drift apart.
# BlueZ is not restarted: that would drop a playing Bluetooth speaker and, by
# OpenRC's stop cascade, the call and cast services with it. Its config is
# rewritten, and biscuit-persist re-derives all of these from the name at boot.
NAME_SERVICES = ("avahi-daemon", "biscuit-voice-assistant", "biscuit-sendspin")
AVAHI_CONF = "/etc/avahi/avahi-daemon.conf"
BLUEZ_CONF = "/etc/bluetooth/main.conf"


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


def _log(fmt, *args):
    sys.stderr.write("[netcfg] " + (fmt % args if args else fmt) + "\n")
    sys.stderr.flush()


def _run(argv, timeout=10, stdin=None):
    """One command, never raising. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, input=stdin)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as err:
        return 1, "", str(err)


# ---------------------------------------------------------------------------
# Wi-Fi
# ---------------------------------------------------------------------------

class WpaCtrl:
    """A direct client for wpa_supplicant's control socket.

    WHY NOT wpa_cli. Joining a network means sending the pre-shared key, and
    wpa_cli takes it as a command-line argument, where it is visible in /proc to
    every process on the device for as long as the call runs. The control
    protocol is half a page of plain text over a unix datagram socket, so
    talking to it directly costs less than the workaround would and keeps the
    key in this process's memory.

    Each instance binds its own socket, named with the pid and a uuid;
    wpa_supplicant replies to the bound address, so two concurrent requests from
    the settings server's thread pool cannot read each other's answers.

    That socket used to be bound under /tmp. The same PSK this class exists to
    keep out of /proc travels over it, in a SET_NETWORK command, so the endpoint
    belongs somewhere only root can reach - not in a world-writable directory
    whose surrounding path is under someone else's control.
    """

    def __init__(self, iface=WPA_IFACE, timeout=6.0):
        self.path = os.path.join(WPA_CTRL_DIR, iface)
        self.local = os.path.join(
            WPA_CLIENT_DIR, ".wpa-%d-%s" % (os.getpid(), uuid.uuid4().hex))
        self.sock = None
        self.timeout = timeout
        self.events = []

    def __enter__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.settimeout(self.timeout)
        # 0700: this is the directory the bound endpoint lives in, and the whole
        # point of moving off /tmp is that nothing else can reach it.
        os.makedirs(WPA_CLIENT_DIR, exist_ok=True)
        os.chmod(WPA_CLIENT_DIR, 0o700)
        # An abstract socket would avoid the unlink dance, but wpa_supplicant
        # rejects one from a client it did not start.
        try:
            os.unlink(self.local)
        except OSError:
            pass
        self.sock.bind(self.local)
        self.sock.connect(self.path)
        return self

    def __exit__(self, *exc):
        try:
            if self.sock:
                self.sock.close()
        finally:
            try:
                os.unlink(self.local)
            except OSError:
                pass
        return False

    def cmd(self, command):
        """Send one command, return its reply as text.

        Unsolicited event messages start with '<' and can arrive between a
        request and its answer, so they are skipped rather than returned as the
        reply to whatever was asked.
        """
        self.sock.send(command.encode())
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.001, deadline - time.monotonic()))
            data = self.sock.recv(8192).decode("utf-8", "replace")
            if data.startswith("<"):
                self.events.append(data)
                continue
            return data
        raise TimeoutError("no reply to %r" % command.split()[0])

    def wait_scan(self, timeout=10.0):
        """Wait on an attached monitor, including events received with ACKs."""
        deadline = time.monotonic() + timeout
        while True:
            if self.events:
                event = self.events.pop(0)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout"
                self.sock.settimeout(remaining)
                try:
                    event = self.sock.recv(8192).decode("utf-8", "replace")
                except socket.timeout:
                    return "timeout"
            if "CTRL-EVENT-SCAN-RESULTS" in event:
                return "complete"
            if "CTRL-EVENT-SCAN-FAILED" in event:
                return "failed"


def wpa_available():
    return os.path.exists(os.path.join(WPA_CTRL_DIR, WPA_IFACE))


def _wpa(command):
    with WpaCtrl() as c:
        return c.cmd(command)


def _kv_block(text):
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _signal_bars(dbm):
    """A 0-4 strength, the way every other Wi-Fi picker shows it.

    Raw dBm is meaningless to most people and the exact thresholds are a matter
    of taste; these are NetworkManager's, so the bars match what the same
    network shows on a phone standing in the same place.
    """
    try:
        dbm = int(dbm)
    except (TypeError, ValueError):
        return 0
    if dbm >= -55:
        return 4
    if dbm >= -66:
        return 3
    if dbm >= -77:
        return 2
    if dbm >= -88:
        return 1
    return 0


def _security_of(flags):
    """A human label for a scan result's flag string."""
    if "WPA3" in flags or "SAE" in flags:
        return "WPA3"
    if "WPA2" in flags or "RSN" in flags:
        return "WPA2"
    if "WPA" in flags:
        return "WPA"
    if "WEP" in flags:
        return "WEP"
    return "Open"


def wifi_status():
    """The current association, or a stub when the radio is not up.

    Deliberately does NOT return the PSK of anything. The page never needs it,
    and a settings page that will hand back the Wi-Fi password to any
    authenticated session is a settings page that leaks it to whoever borrowed
    the browser.
    """
    if not wpa_available():
        return {"available": False, "state": "unavailable", "ssid": "",
                "ip": "", "signal": 0, "freq": 0, "band": ""}
    try:
        st = _kv_block(_wpa("STATUS"))
    except (OSError, TimeoutError) as err:
        _log("STATUS failed: %s", err)
        return {"available": False, "state": "unavailable", "ssid": "",
                "ip": "", "signal": 0, "freq": 0, "band": ""}

    freq = 0
    try:
        freq = int(st.get("freq", 0))
    except ValueError:
        pass

    # The associated AP's signal comes from SIGNAL_POLL, not STATUS.
    rssi = None
    if st.get("wpa_state") == "COMPLETED":
        try:
            poll = _kv_block(_wpa("SIGNAL_POLL"))
            rssi = int(poll.get("RSSI", 0)) or None
        except (OSError, TimeoutError, ValueError):
            pass

    # STATUS reports the negotiated key management rather than the AP's
    # advertised flags, so it is the honest answer for "how is THIS link
    # secured" - which is not always what the scan row said.
    key_mgmt = st.get("key_mgmt", "")
    if "SAE" in key_mgmt:
        security = "WPA3"
    elif "WPA2" in key_mgmt or "PSK" in key_mgmt:
        security = "WPA2"
    elif key_mgmt in ("NONE", ""):
        security = "Open" if st.get("wpa_state") == "COMPLETED" else ""
    else:
        security = key_mgmt

    return {
        "available": True,
        "state": st.get("wpa_state", "UNKNOWN"),
        "associated": st.get("wpa_state") == "COMPLETED",
        "connected": st.get("wpa_state") == "COMPLETED" and bool(st.get("ip_address")),
        "ssid": st.get("ssid", ""),
        "bssid": st.get("bssid", ""),
        "ip": st.get("ip_address", ""),
        "mac": st.get("address", ""),
        "freq": freq,
        "band": "5 GHz" if freq >= 4900 else ("2.4 GHz" if freq else ""),
        "rssi": rssi,
        "signal": _signal_bars(rssi) if rssi is not None else 0,
        "security_label": security,
    }


def wifi_saved():
    """Networks wpa_supplicant already has credentials for."""
    if not wpa_available():
        return []
    try:
        text = _wpa("LIST_NETWORKS")
    except (OSError, TimeoutError):
        return []
    out = []
    for line in text.splitlines()[1:]:            # first line is the header
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        nid, ssid, _bssid, flags = parts[0], parts[1], parts[2], parts[3]
        if not nid.isdigit():
            continue
        out.append({
            "id": int(nid),
            "ssid": ssid,
            "current": "[CURRENT]" in flags,
            "disabled": "[DISABLED]" in flags,
        })
    return out


def wifi_scan(wait=True, with_status=False):
    """Ask for a fresh scan, then return what is on the air.

    Results are returned even when the scan request itself is refused:
    wpa_supplicant answers FAIL-BUSY when it is already scanning, which is the
    normal case on a device that is disconnected and retrying, and the cached
    results from that in-flight scan are exactly what the caller wanted.
    """
    scan = {"state": "unavailable", "cached": True}
    def result(networks):
        return {"networks": networks, "scan": scan} if with_status else networks
    if not wpa_available():
        return result([])
    try:
        with WpaCtrl() as c:
            # Association can stay COMPLETED during a background scan. Attach
            # before requesting it so even an immediate completion is retained.
            if wait:
                with WpaCtrl() as monitor:
                    attached = monitor.cmd("ATTACH").strip() == "OK"
                    reply = c.cmd("SCAN").strip()
                    if attached and reply in ("OK", "FAIL-BUSY"):
                        scan["state"] = monitor.wait_scan()
                    else:
                        scan["state"] = "failed"
            else:
                scan["state"] = "pending" if c.cmd("SCAN").strip() == "OK" else "failed"
            scan["cached"] = scan["state"] != "complete"
            text = c.cmd("SCAN_RESULTS")
    except (OSError, TimeoutError) as err:
        _log("scan failed: %s", err)
        scan["state"] = "failed"
        return result([])

    seen = {}
    detected_bands = {}
    for line in text.splitlines()[1:]:            # header
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        bssid, freq, level, flags, ssid = parts[0], parts[1], parts[2], parts[3], parts[4]
        if not ssid:
            continue                              # hidden; joined by name instead
        try:
            freq_i, level_i = int(freq), int(level)
        except ValueError:
            continue
        # Keep every detected band before choosing the strongest BSSID.
        # A stronger 2.4 GHz AP must not hide the same SSID on 5 GHz.
        band = "5 GHz" if freq_i >= 4900 else "2.4 GHz"
        detected_bands.setdefault(ssid, set()).add(band)
        # One row per network preserves mesh roaming; selecting this row
        # still joins the SSID without pinning a band or BSSID.
        prev = seen.get(ssid)
        if prev and prev["rssi"] >= level_i:
            continue
        seen[ssid] = {
            "ssid": ssid,
            "bssid": bssid,
            "rssi": level_i,
            "signal": _signal_bars(level_i),
            "freq": freq_i,
            "band": "5 GHz" if freq_i >= 4900 else "2.4 GHz",
            "security": _security_of(flags),
            "open": _security_of(flags) == "Open",
        }
    for ssid, network in seen.items():
        network["bands"] = sorted(detected_bands[ssid])
    return result(sorted(seen.values(), key=lambda n: -n["rssi"]))


def _wpa_quote(value):
    """A wpa_supplicant string literal.

    SET_NETWORK takes a quoted string whose only escapes are backslash and
    double quote. Anything else is passed through, so a PSK with spaces or a $
    in it arrives intact.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def wifi_connect(ssid, psk=None, hidden=False):
    """Join a network, replacing any saved entry for the same name.

    Returns (ok, message). The caller polls wifi_status() for the outcome
    rather than blocking here: association plus DHCP can take fifteen seconds
    on this radio, which is far longer than a settings page should hold a
    request open.
    """
    if not wpa_available():
        return False, "Wi-Fi is not running on this device."
    if not ssid:
        return False, "A network name is required."
    if psk is not None and psk != "" and not 8 <= len(psk) <= 63:
        return False, "A Wi-Fi password must be 8 to 63 characters."

    try:
        with WpaCtrl(timeout=10.0) as c:
            # Replace rather than accumulate: rejoining a network with a
            # corrected password otherwise leaves the old entry in the file,
            # and wpa_supplicant will happily keep retrying the wrong one.
            for net in wifi_saved():
                if net["ssid"] == ssid:
                    c.cmd("REMOVE_NETWORK %d" % net["id"])

            nid = c.cmd("ADD_NETWORK").strip()
            if not nid.isdigit():
                return False, "Could not add the network."

            def setnet(key, value):
                return c.cmd("SET_NETWORK %s %s %s" % (nid, key, value)).startswith("OK")

            if not setnet("ssid", _wpa_quote(ssid)):
                c.cmd("REMOVE_NETWORK %s" % nid)
                return False, "Could not set the network name."
            if hidden:
                setnet("scan_ssid", "1")
            if psk:
                if not setnet("psk", _wpa_quote(psk)):
                    c.cmd("REMOVE_NETWORK %s" % nid)
                    return False, "That password was not accepted by the Wi-Fi driver."
            else:
                setnet("key_mgmt", "NONE")

            c.cmd("ENABLE_NETWORK %s" % nid)
            c.cmd("SELECT_NETWORK %s" % nid)      # also disables the others
            # update_config=1 is set in the shipped wpa_supplicant.conf, so this
            # writes through the persist bind mount.
            if not c.cmd("SAVE_CONFIG").startswith("OK"):
                _log("SAVE_CONFIG refused; the join is live but will not survive a reboot")
            _protect_wpa_conf()
            c.cmd("RECONNECT")
    except (OSError, TimeoutError) as err:
        return False, "Wi-Fi did not respond: %s" % err
    return True, "Connecting to %s." % ssid


def _protect_wpa_conf():
    """Re-assert 0600 on the file wpa_supplicant just rewrote.

    SAVE_CONFIG makes wpa_supplicant write the config itself, under its own 0022
    umask, so the result is 0644. That silently discards the `chmod 600`
    biscuit-setup.sh applies when it first writes the file, and it happens on
    every join and every forget issued from the settings page.

    The file holds the PSK in the clear, and /etc/wpa_supplicant is the persist
    bind mount, so the exposure outlives a reflash too. Measured on Device 1
    before this existed: 644 root:root, readable by the unprivileged account.

    Best effort on purpose: the network is already joined by the time this runs,
    and failing a join over a chmod would be the worse outcome.
    """
    try:
        os.chmod(WPA_CONF, 0o600)
    except OSError as err:
        _log("could not restrict %s: %s", WPA_CONF, err)


def wifi_forget(nid):
    if not wpa_available():
        return False, "Wi-Fi is not running on this device."
    try:
        with WpaCtrl() as c:
            if not c.cmd("REMOVE_NETWORK %d" % int(nid)).startswith("OK"):
                return False, "No such saved network."
            c.cmd("SAVE_CONFIG")
            _protect_wpa_conf()
    except (OSError, TimeoutError, ValueError) as err:
        return False, "Wi-Fi did not respond: %s" % err
    return True, "Network forgotten."


def wifi_select(nid):
    """Switch to an already-saved network."""
    if not wpa_available():
        return False, "Wi-Fi is not running on this device."
    try:
        with WpaCtrl() as c:
            if not c.cmd("SELECT_NETWORK %d" % int(nid)).startswith("OK"):
                return False, "No such saved network."
    except (OSError, TimeoutError, ValueError) as err:
        return False, "Wi-Fi did not respond: %s" % err
    return True, "Switching network."


def wifi_reconnect():
    if not wpa_available():
        return False, "Wi-Fi is not running on this device."
    try:
        _wpa("RECONNECT")
    except (OSError, TimeoutError) as err:
        return False, str(err)
    return True, "Reconnecting."


# ---------------------------------------------------------------------------
# Bluetooth
# ---------------------------------------------------------------------------

# Audio class-of-device major/minor values, used to tell "a speaker we can play
# to" from "a phone that plays to us". BlueZ reports the peer's class in `info`,
# and the UUID list says which profiles it actually offers - a phone advertises
# A2DP Source, a speaker advertises A2DP Sink. The UUIDs are authoritative and
# the class is the fallback for a peer that has not been queried yet.
A2DP_SINK_UUID = "0000110b"       # peer can receive audio  -> we play to it
A2DP_SOURCE_UUID = "0000110a"     # peer can send audio     -> it plays to us
HFP_UUIDS = ("0000111e", "0000111f")

BT_TIMEOUT = 12


def _btctl(*args, timeout=BT_TIMEOUT):
    rc, out, err = _run(["bluetoothctl"] + list(args), timeout=timeout)
    return rc, out + err


def bt_available():
    rc, out = _btctl("show", timeout=6)
    return rc == 0 and "Controller" in out


def bt_adapter():
    """Adapter identity and the three states that matter to a user."""
    rc, out = _btctl("show", timeout=6)
    info = {"available": rc == 0 and "Controller" in out,
            "alias": "", "address": "", "powered": False,
            "discoverable": False, "pairable": False, "discovering": False}
    if not info["available"]:
        return info
    m = re.search(r"Controller\s+([0-9A-F:]{17})", out)
    if m:
        info["address"] = m.group(1)
    for key, field in (("Alias", "alias"), ("Powered", "powered"),
                       ("Discoverable", "discoverable"), ("Pairable", "pairable"),
                       ("Discovering", "discovering")):
        m = re.search(r"^\s*%s:\s*(.+)$" % key, out, re.M)
        if not m:
            continue
        value = m.group(1).strip()
        info[field] = value if field == "alias" else value == "yes"
    return info


def bt_audio_transports():
    """MACs that bluealsa currently holds an audio transport for.

    WHY BlueZ's `Connected: yes` IS NOT ENOUGH. That flag reports the ACL link,
    and a speaker that loses power abruptly - switched off at the wall, battery
    flat, carried out of range - does not get to send a disconnect. The
    controller only notices at the link supervision timeout, which is about
    twenty seconds, and BlueZ keeps answering "connected" until then. For a
    settings page that is long enough to be read as simply wrong.

    bluealsa is the better witness for audio specifically: it holds a PCM per
    profile per device, and it drops the PCM when the transport goes. Crossing
    the two means the page can say "connected but no audio link" rather than
    claiming a speaker is playing when it is not.

    Returns {mac: set(of directions)} where a direction is "sink" (we play to
    it) or "source" (it plays to us).
    """
    rc, out, _ = _run(["bluealsa-cli", "list-pcms"], timeout=8)
    if rc != 0:
        return {}
    found = {}
    for line in out.splitlines():
        line = line.strip()
        # /org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsrc/sink
        m = re.search(r"/dev_([0-9A-Fa-f_]{17})/(\w+)/(\w+)", line)
        if not m:
            continue
        mac = m.group(1).replace("_", ":").upper()
        # The direction is named from bluealsa's point of view: a "sink" PCM on
        # an a2dp-source profile is where WE write audio for the peer to play.
        found.setdefault(mac, set()).add(m.group(3))
    return found


def _bt_info(mac, transports=None):
    """Everything BlueZ knows about one peer, crossed with bluealsa's view."""
    rc, out = _btctl("info", mac, timeout=8)
    if rc != 0 or "Device" not in out:
        return None
    uuids = [u.lower() for u in re.findall(r"UUID:.*?\(([0-9a-fA-F]{8})-", out)]

    def flag(name):
        m = re.search(r"^\s*%s:\s*(yes|no)\s*$" % name, out, re.M)
        return bool(m and m.group(1) == "yes")

    name = mac
    m = re.search(r"^\s*(?:Alias|Name):\s*(.+)$", out, re.M)
    if m:
        name = m.group(1).strip()

    icon = ""
    m = re.search(r"^\s*Icon:\s*(.+)$", out, re.M)
    if m:
        icon = m.group(1).strip()

    # "Can we play to it" and "can it play to us" are independent, and a pair of
    # headphones with a microphone is legitimately both. The page shows the
    # direction rather than making the user infer it from a device name.
    plays_to_us = A2DP_SOURCE_UUID in uuids
    we_play_to = A2DP_SINK_UUID in uuids
    if not plays_to_us and not we_play_to:
        # Not yet queried, or a non-audio peer. Guess from the icon so a freshly
        # discovered speaker is not shown as having no audio role at all.
        if icon in ("audio-headset", "audio-headphones", "audio-card"):
            we_play_to = True
        elif icon in ("phone", "computer"):
            plays_to_us = True

    if transports is None:
        transports = bt_audio_transports()
    dirs = transports.get(mac.upper(), set())
    connected = flag("Connected")

    return {
        "mac": mac,
        "name": name,
        "icon": icon,
        "paired": flag("Paired"),
        "trusted": flag("Trusted"),
        "blocked": flag("Blocked"),
        "connected": connected,
        # The honest audio state: is an audio transport actually open.
        #
        # `link_only` means CONNECTED WITH NO AUDIO PROFILE OPEN - a phone linked
        # for the Bluetooth proxy or for hands-free, or a peer that has paired
        # and linked but never opened A2DP. It is a normal state, not a fault.
        #
        # It does NOT mean "a speaker that was switched off", which is what this
        # comment claimed for a long time. That reading assumed the audio
        # transport drops immediately while the ACL link lingers to the
        # supervision timeout about twenty seconds later, leaving a window to
        # display. Measured on 2026-09-28 by cutting a speaker's power at the
        # wall, twice - once with a client holding the PCM and once with
        # nothing holding it - `link_only` never appeared: the device went from
        # connected to gone within a single one-second sample, at t=+34.1s and
        # t=+36.7s.
        #
        # There is no window because there is no second event source. bluealsa's
        # transports ARE BlueZ MediaTransport1 objects, so `dirs` is derived
        # from BlueZ's own view and cannot drop before BlueZ marks the device
        # disconnected. A dying speaker therefore cannot produce this state.
        #
        # It IS reachable, briefly: sampling a reconnect at 5 Hz caught
        # `connected and not dirs` for about half a second between the ACL
        # link coming up and the audio profiles negotiating. That is what the
        # badge shows, and why it must not be styled as a warning - it appears
        # on every ordinary connection.
        "audio_ready": bool(dirs),
        "link_only": connected and not dirs,
        "battery": _bt_battery(out),
        "plays_to_us": plays_to_us,
        "we_play_to": we_play_to,
        "handsfree": any(u in uuids for u in HFP_UUIDS),
    }


def _bt_battery(info_text):
    m = re.search(r"Battery Percentage:.*?\((\d+)\)", info_text)
    return int(m.group(1)) if m else None


def _bt_list(which=None):
    args = ["devices"] + ([which] if which else [])
    rc, out = _btctl(*args, timeout=8)
    if rc != 0:
        return []
    return re.findall(r"^Device\s+([0-9A-F:]{17})\s+(.*)$", out, re.M)


def bt_devices():
    """Paired peers, each with its role and connection state.

    bluealsa is asked ONCE and the answer passed down, rather than per device:
    each `bluealsa-cli list-pcms` is a round trip, and this already pays for one
    `bluetoothctl info` per peer.
    """
    transports = bt_audio_transports()
    out = []
    for mac, name in _bt_list("Paired"):
        info = _bt_info(mac, transports)
        if info is None:
            info = {"mac": mac, "name": name, "paired": True, "trusted": False,
                    "connected": False, "audio_ready": False, "link_only": False,
                    "plays_to_us": False, "we_play_to": False,
                    "handsfree": False, "battery": None, "icon": ""}
        out.append(info)
    # Devices that are actually carrying audio first, then merely linked, then
    # the rest - so the top of the list is always the one in use.
    return sorted(out, key=lambda d: (not d.get("audio_ready"),
                                      not d["connected"], d["name"].lower()))


def bt_connected_macs():
    """Just the connected addresses.

    Cheap on purpose: the root settings list wants a count, and running
    `bluetoothctl info` for every paired peer to get one - which is what
    bt_devices() does - costs about a second per device on this CPU.
    """
    return [mac for mac, _ in _bt_list("Connected")]


def bt_discovered():
    """Peers seen on the air that are not already paired."""
    paired = {mac for mac, _ in _bt_list("Paired")}
    transports = bt_audio_transports()
    out = []
    for mac, name in _bt_list():
        if mac in paired:
            continue
        # A peer with no name yet is an address and nothing else, which is not
        # something a user can recognise; skip it rather than offering a row
        # that says "68:54:FD:...".
        if not name or re.fullmatch(r"[0-9A-F-]{17}", name):
            continue
        info = _bt_info(mac, transports) or {"mac": mac, "name": name}
        info.setdefault("paired", False)
        out.append(info)
    return sorted(out, key=lambda d: d.get("name", "").lower())


# Shared exclusion between this portal and biscuit-btproxy. A flag file named
# after its owner means "a pairing or connection is in progress; discovery will
# disturb it". The file holds the owner's PID so a crashed holder does not wedge
# the other side forever.
#
# The contract deliberately covers CONNECTS ONLY, not scans. Concurrent
# discovery was measured on this controller: two independent BlueZ clients each
# running an LE-filtered discovery saw ~26 adv/s alone and ~21 adv/s together,
# and stopping one left the other's scan running. BlueZ reference-counts
# discovery per client and the sessions genuinely coexist.
#
# What does interfere is the transport. An UNFILTERED discovery - `scan on`,
# which is what this portal needs to find speakers - makes BlueZ interleave
# BR/EDR inquiry with LE scanning, and inquiry monopolises the radio: measured
# as roughly 6 s of advertisements followed by 10 s of near-silence, repeating.
# That is a duty-cycle cost the proxy simply absorbs while a user-initiated scan
# runs, not a conflict to arbitrate. Connects are the real hazard, because a
# connection attempt raised while discovery is active fails with
# le-connection-abort-by-local.
BT_BUSY_DIR = "/run/biscuit-bt/busy"


def _bt_busy_holders(ignore=None):
    """Owners currently holding the connect flag, skipping dead ones."""
    held = []
    try:
        names = os.listdir(BT_BUSY_DIR)
    except OSError:
        return held
    for name in names:
        if name == ignore:
            continue
        path = os.path.join(BT_BUSY_DIR, name)
        try:
            with open(path) as fh:
                pid = int((fh.read().strip() or "0"))
        except (OSError, ValueError):
            pid = 0
        if pid > 0 and not os.path.exists("/proc/%d" % pid):
            # The holder died without cleaning up.
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        held.append(name)
    return held


@contextlib.contextmanager
def _bt_busy(owner="netcfg"):
    """Hold the connect flag for the duration of a pairing or connection."""
    path = os.path.join(BT_BUSY_DIR, owner)
    try:
        os.makedirs(BT_BUSY_DIR, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(str(os.getpid()))
    except OSError as err:
        _log("could not take the bt busy flag: %s", err)
        path = None
    try:
        yield
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def bt_scan(seconds=8):
    """Run a bounded discovery and return what turned up.

    bluetoothctl's own --timeout is used rather than a background scan plus a
    stop: an abandoned scan keeps the radio busy, and on this device that costs
    Wi-Fi throughput for as long as it runs.

    If the proxy is mid-connect, give it a moment to finish rather than starting
    discovery underneath it. The wait is short and then the scan runs anyway -
    the user pressed a button, so a slow answer beats no answer.
    """
    seconds = max(3, min(30, int(seconds)))
    waited = 0.0
    while _bt_busy_holders(ignore="netcfg") and waited < 3.0:
        time.sleep(0.25)
        waited += 0.25
    _run(["bluetoothctl", "--timeout", str(seconds), "scan", "on"],
         timeout=seconds + 6)
    return bt_discovered()


def bt_pair(mac):
    """Pair, trust and connect in one step.

    Trust is not optional here. The device has no display or keypad, so a peer
    that is paired but not trusted has every later reconnection prompt go
    unanswered until it times out - which is the failure biscuit-btaudio.sh
    documents at length. A user who pressed "Pair" has already made the trust
    decision.
    """
    if not _valid_mac(mac):
        return False, "Not a Bluetooth address."
    with _bt_busy():
        rc, out = _btctl("pair", mac, timeout=25)
        if rc != 0 and "AlreadyExists" not in out:
            return False, _bt_error(out, "Pairing failed.")
        _btctl("trust", mac, timeout=8)
        rc, out = _btctl("connect", mac, timeout=20)
    if rc != 0:
        return True, "Paired, but it did not connect. Try Connect once it is awake."
    return True, "Paired and connected."


def bt_connect(mac):
    if not _valid_mac(mac):
        return False, "Not a Bluetooth address."
    with _bt_busy():
        rc, out = _btctl("connect", mac, timeout=20)
    if rc != 0:
        return False, _bt_error(out, "Could not connect.")
    return True, "Connected."


def bt_disconnect(mac):
    if not _valid_mac(mac):
        return False, "Not a Bluetooth address."
    rc, out = _btctl("disconnect", mac, timeout=15)
    if rc != 0:
        return False, _bt_error(out, "Could not disconnect.")
    return True, "Disconnected."


def bt_remove(mac):
    if not _valid_mac(mac):
        return False, "Not a Bluetooth address."
    rc, out = _btctl("remove", mac, timeout=15)
    if rc != 0:
        return False, _bt_error(out, "Could not remove that device.")
    return True, "Removed."


# Which speaker this device plays through, and whether it is doing so right now.
# The target is a preference and lives on the persist partition; the active file
# is biscuit-btcast's own state and lives in /run, because "is it casting" is
# only true while that service and the connection are both up.
CAST_TARGET = "/opt/persist/btcast"
CAST_ACTIVE = "/run/biscuit-btcast/active"


def cast_target():
    try:
        with open(CAST_TARGET) as f:
            mac = f.read().strip()
        return mac if _valid_mac(mac) else ""
    except OSError:
        return ""


def cast_active():
    try:
        with open(CAST_ACTIVE) as f:
            return f.read().strip()
    except OSError:
        return ""


def set_cast_target(mac):
    """Choose the speaker to play through, or pass a falsy value to stop.

    Only writes the preference. biscuit-btcast polls this file and owns
    everything else - starting the bridge, muting the internal speaker, and
    putting both back when the speaker disconnects - so that a cast survives a
    reboot and resumes by itself when the speaker comes back into range.
    """
    if mac:
        if not _valid_mac(mac):
            return False, "Not a Bluetooth address."
        info = _bt_info(mac)
        if info is None or not info.get("paired"):
            return False, "That device is not paired."
        if not info.get("we_play_to"):
            return False, ("%s does not accept audio. Pair a speaker or "
                           "headphones instead." % info.get("name", mac))
    try:
        os.makedirs(os.path.dirname(CAST_TARGET), exist_ok=True)
        tmp = CAST_TARGET + ".tmp"
        with open(tmp, "w") as f:
            f.write((mac or "") + "\n")
            f.flush()
            os.fsync(f.fileno())
        _durable_replace(tmp, CAST_TARGET)
    except OSError as err:
        return False, "Could not save the choice: %s" % err
    if not mac:
        return True, "Playing through this device again."
    return True, "Playing through %s." % (_bt_info(mac) or {}).get("name", mac)


def bt_set_alias(alias):
    """Rename the adapter, which is the name phones show in their pairing list.

    Not persisted here: BlueZ writes the alias into /var/lib/bluetooth, which
    biscuit-persist bind-mounts onto the persist partition.
    """
    alias = (alias or "").strip()
    # Empty, or the device name itself, means "follow the device name": the
    # alias is dropped and BlueZ falls back to main.conf's Name, which
    # biscuit-persist keeps equal to the device name. Pinning it as an alias
    # instead would stop it following the next rename.
    if not alias or alias == device_name():
        rc, out = _btctl("reset-alias", timeout=8)
        if rc != 0:
            return False, _bt_error(out, "Could not reset the Bluetooth name.")
        return True, "The Bluetooth name now follows the device name."
    if len(alias) > 64:
        return False, "A Bluetooth name must be 1 to 64 characters."
    if not re.fullmatch(r"[\w .,'()\[\]+&@!-]+", alias):
        return False, "That name has characters Bluetooth cannot carry."
    rc, out = _btctl("system-alias", alias, timeout=8)
    if rc != 0:
        return False, _bt_error(out, "Could not set the Bluetooth name.")
    return True, "Bluetooth name set to %s." % alias


def _valid_mac(mac):
    return bool(mac and re.fullmatch(r"[0-9A-Fa-f:]{17}", mac))


def _bt_error(out, fallback):
    """BlueZ's own reason, when it gave one worth showing."""
    m = re.search(r"Failed to \w+:\s*(.+)", out)
    if m:
        reason = m.group(1).strip()
        friendly = {
            "org.bluez.Error.AuthenticationCanceled": "The other device cancelled pairing.",
            "org.bluez.Error.AuthenticationFailed": "Pairing was rejected by the other device.",
            "org.bluez.Error.AuthenticationTimeout": "The other device did not answer in time.",
            "org.bluez.Error.ConnectionAttemptFailed": "It did not answer. Make sure it is awake and in range.",
            "org.bluez.Error.NotReady": "The Bluetooth adapter is not ready.",
            "org.bluez.Error.InProgress": "Another Bluetooth operation is still running.",
        }
        return friendly.get(reason, reason)
    return fallback


# ---------------------------------------------------------------------------
# Account and device identity
# ---------------------------------------------------------------------------

def _is_login_account(entry):
    """A real person's account, as opposed to a daemon's.

    Alpine's system users sit below 1000 and `nobody` sits at 65534, so the
    range plus a real shell is enough. The stock postmarketOS account is
    excluded because biscuit-setup.sh locks it once a real one exists, and
    offering to change the password of a locked account is a dead end.
    """
    if not 1000 <= entry.pw_uid < 65534:
        return False
    if entry.pw_shell in ("/sbin/nologin", "/bin/false", "/usr/sbin/nologin"):
        return False
    return True


def accounts():
    return [e for e in pwd.getpwall() if _is_login_account(e)]


def primary_account():
    """The account this device belongs to.

    After setup there is exactly one; before it there is only the stock one.
    Preferring a non-stock name means the page shows the real account as soon as
    setup has made one, without needing to read biscuit-setup's state.
    """
    users = accounts()
    for e in users:
        if e.pw_name != STOCK_ACCOUNT:
            return e
    return users[0] if users else None


def _groups_of(name):
    rc, out, _ = _run(["id", "-nG", name], timeout=5)
    return out.split() if rc == 0 else []


def user_info():
    """Who is signed in to this device, for the About page."""
    entry = primary_account()
    if entry is None:
        return {"exists": False}
    groups = _groups_of(entry.pw_name)
    # gecos is "Full Name,room,work,home" by convention; only the first field is
    # ever set here, and adduser -D leaves it empty.
    full_name = (entry.pw_gecos or "").split(",")[0].strip()
    return {
        "exists": True,
        "username": entry.pw_name,
        "full_name": full_name,
        "uid": entry.pw_uid,
        "home": entry.pw_dir,
        "shell": entry.pw_shell,
        "groups": groups,
        "admin": any(g in groups for g in ("wheel", "root", "sudo")),
        "provisioned": os.path.exists(PROVISIONED),
        "stock_account_active": _stock_account_active(),
        "password_age_days": _password_age_days(entry.pw_name),
    }


def _shadow_entry(name):
    """One account's (hash, last-change-in-days) from /etc/shadow.

    Read directly rather than via `passwd -S`, which busybox does not implement:
    the image's passwd only takes -a/-d/-l/-u, so the -S form silently reported
    every account as locked.
    """
    try:
        with open("/etc/shadow") as f:
            for line in f:
                parts = line.rstrip("\n").split(":")
                if len(parts) > 2 and parts[0] == name:
                    try:
                        changed = int(parts[2])
                    except ValueError:
                        changed = None
                    return parts[1], changed
    except OSError:
        pass
    return None, None


def _stock_account_active():
    """True when postmarketOS's shipped account can still be logged into.

    A device where setup never ran still answers to user/147147, which is a
    published default, so the About page says so rather than leaving it to be
    discovered. Only meaningful when a DIFFERENT account is the real one - if
    the owner kept the name `user` during setup, this is their account and its
    password is their own.
    """
    entry = primary_account()
    if entry is not None and entry.pw_name == STOCK_ACCOUNT:
        return False
    hashed, _changed = _shadow_entry(STOCK_ACCOUNT)
    if hashed is None:
        return False
    # busybox and shadow both disable an account by prefixing the hash with '!'
    # or replacing it with '*'; an empty field means no password at all, which
    # is worse rather than better.
    return not hashed.startswith("!") and hashed not in ("*", "x")


def _password_age_days(name):
    _hashed, changed = _shadow_entry(name)
    if not changed:
        return None
    # Setup creates the account while the clock still reads 2010 - the RTC's
    # start - so shadow records a change date sixteen years ago and the page
    # said "Last changed 6114 days ago". A date earlier than this file's own
    # build cannot be real; report it as unknown instead.
    try:
        built = int(os.stat(__file__).st_mtime / 86400)
    except OSError:
        built = 0
    if changed < built:
        return None
    return max(0, int(time.time() / 86400) - changed)


def _persist_account():
    """Snapshot the owner account into the persist store.

    Best effort and deliberately quiet: a password change that actually
    succeeded must not be reported as a failure because the snapshot did not.
    The worst case is that biscuit-persist catches up at the next boot.

    SSH keys need no equivalent - ~/.ssh is bind-mounted from the store, so
    writes through the SSH page land in the persistent copy already.
    """
    try:
        rc, _out, err = _run(["/usr/bin/biscuit-persist-account", "save"],
                             timeout=10)
        if rc != 0:
            _log("account snapshot failed: %s", (err or "").strip())
    except Exception as err:  # noqa: BLE001
        _log("could not snapshot the account: %s", err)


def set_password(username, new_password):
    """Change an account's password.

    The caller is responsible for having verified the CURRENT password first -
    this module has no session to check it against. biscuit-settings does that
    with the same verify_password() the login form uses.

    chpasswd reads from stdin, so the new password never appears in argv where
    /proc would show it to anything else running on the device.
    """
    if not username or username not in {e.pw_name for e in accounts()}:
        return False, "No such account."
    if not isinstance(new_password, str) or len(new_password) < 8:
        return False, "Choose a password of at least 8 characters, as setup does."
    if len(new_password) > 512:
        return False, "That password is too long."
    if "\n" in new_password or ":" in new_password:
        return False, "A password cannot contain a colon or a line break."
    rc, _out, err = _run(["chpasswd"], timeout=15,
                         stdin="%s:%s\n" % (username, new_password))
    if rc != 0:
        _log("chpasswd failed: %s", err.strip())
        return False, "The system refused the password change."
    # Re-snapshot the account so the new hash survives the next flash.
    # biscuit-persist does this at boot too, but a password changed here and
    # then flashed before the next reboot would otherwise come back as the old
    # one - silently, and only discovered when the owner cannot log in.
    _persist_account()
    _refresh_ssh_policy()
    return True, "Password changed."


def _refresh_ssh_policy():
    """Have biscuit-firstboot look at the shipped account's password again.

    While `user` has the published password, sshd refuses password logins for
    it off the USB cable, and the settings page refuses it there too. That
    rule is written at boot; without this, a password changed here left it in
    place until the next one. Asked after every change, whichever account: it
    is cheap, and it reloads sshd only when its rule actually changes. Best
    effort, like the snapshot above - the password did change.
    """
    try:
        rc, _out, err = _run(["rc-service", "biscuit-firstboot", "sshpolicy"],
                             timeout=20)
        if rc != 0:
            _log("sshpolicy failed: %s", (err or "").strip())
    except Exception as err:  # noqa: BLE001
        _log("could not re-run the SSH policy: %s", err)


def set_full_name(username, full_name):
    """Set the account's display name, the first gecos field."""
    if not username or username not in {e.pw_name for e in accounts()}:
        return False, "No such account."
    full_name = (full_name or "").strip()
    if len(full_name) > 64:
        return False, "That name is too long."
    if re.search(r"[:,\n]", full_name):
        return False, "A name cannot contain a colon or a comma."
    rc, _out, err = _run(["usermod", "-c", full_name, username], timeout=10)
    if rc != 0:
        # busybox has no usermod; edit the field in place instead. chfn is not
        # in the image either.
        ok = _rewrite_gecos(username, full_name)
        if not ok:
            _log("usermod failed: %s", err.strip())
            return False, "Could not change the display name."
    return True, "Name updated."


def _rewrite_gecos(username, full_name):
    """Replace one account's gecos field in /etc/passwd, atomically."""
    try:
        with open("/etc/passwd") as f:
            lines = f.readlines()
        out = []
        found = False
        for line in lines:
            parts = line.rstrip("\n").split(":")
            if len(parts) == 7 and parts[0] == username:
                parts[4] = full_name
                line = ":".join(parts) + "\n"
                found = True
            out.append(line)
        if not found:
            return False
        tmp = "/etc/passwd.biscuit-tmp"
        with open(tmp, "w") as f:
            f.writelines(out)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        _durable_replace(tmp, "/etc/passwd")
        return True
    except OSError as err:
        _log("gecos rewrite failed: %s", err)
        return False


VALID_HOSTNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")


def device_name():
    try:
        with open(HOSTNAME_FILE) as f:
            return f.read().strip()
    except OSError:
        return socket.gethostname()


def set_device_name(name):
    """Rename the device everywhere it is published.

    The name reaches four places and they must not drift:
      /etc/hostname          what the shell prompt and mDNS use
      /opt/persist/etc/...   the copy biscuit-persist restores after a flash
      avahi-daemon           <name>.local on the network
      the assistant          the entity name in Home Assistant, passed as --name

    Restarting the assistant costs about ten seconds of deafness, which is why
    this is not offered as a live-typing field.
    """
    name = (name or "").strip()
    if not VALID_HOSTNAME.match(name):
        return False, ("A device name may use letters, numbers and hyphens, "
                       "must start with a letter or number, and be 63 "
                       "characters or fewer.")
    old_name = device_name()
    if name == old_name:
        return True, "That is already the device name."
    # Without an alias of its own the adapter is showing main.conf's Name,
    # which is the old device name; with one, the owner chose a separate
    # Bluetooth name and a rename leaves it alone.
    bt_follows = bt_adapter().get("alias") == old_name

    try:
        for path in (HOSTNAME_FILE, PERSIST_HOSTNAME):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(name + "\n")
                f.flush()
                os.fsync(f.fileno())
            _durable_replace(tmp, path)
    except OSError as err:
        return False, "Could not write the new name: %s" % err

    _run(["hostname", name], timeout=5)
    # Setup pins host-name= in Avahi's config, so the hostname alone no longer
    # reaches <name>.local; and BlueZ reads its name only from its config.
    _set_conf_line(AVAHI_CONF, r"^#?host-name=.*$", "host-name=" + name)
    _set_conf_line(BLUEZ_CONF, r"^#?Name = .*$", "Name = " + name)
    # Detached: restarting the assistant tears down the Home Assistant
    # connection, and holding the HTTP request open for it would time out the
    # page before the answer came back. Only what is running is restarted:
    # restart would otherwise start an app the owner has not installed or
    # has stopped.
    for svc in NAME_SERVICES:
        if _run(["rc-service", "--quiet", svc, "status"], timeout=5)[0] != 0:
            continue
        try:
            subprocess.Popen(["rc-service", svc, "restart"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as err:
            _log("could not restart %s: %s", svc, err)
    msg = "Renamed to %s. The assistant is restarting." % name
    if bt_follows:
        msg += " Bluetooth shows the new name after the next restart."
    return True, msg


def _set_conf_line(path, pattern, line):
    """Replace every line matching pattern with line, durably. Never raises."""
    try:
        with open(path) as f:
            old = f.read()
    except OSError:
        return
    new = re.sub(pattern, line, old, flags=re.M)
    if new == old:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        _durable_replace(tmp, path)
    except OSError as err:
        _log("could not update %s: %s", path, err)


# ---------------------------------------------------------------------------
# System information
# ---------------------------------------------------------------------------

APK_DB = "/lib/apk/db/installed"
_apk_versions = (None, {})


def _apk_version(pkg):
    """The installed version string, e.g. device-amazon-biscuit-6-r121.

    Read from apk's database file rather than by running `apk info`: two of
    those cost the About page a sixth of a second on every load, for two
    strings that only change when a package does. Cached until the database
    changes.
    """
    global _apk_versions
    try:
        mtime = os.stat(APK_DB).st_mtime
    except OSError:
        return ""
    if _apk_versions[0] != mtime:
        found, name = {}, None
        try:
            with open(APK_DB, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("P:"):
                        name = line[2:].rstrip("\n")
                    elif line.startswith("V:") and name:
                        found[name] = line[2:].rstrip("\n")
                        name = None
        except OSError:
            return ""
        _apk_versions = (mtime, found)
    version = _apk_versions[1].get(pkg)
    return "%s-%s" % (pkg, version) if version else ""


def _disk(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if not total:
        return None
    return {"path": path, "total": total, "free": free, "used": total - free,
            "percent": round(100.0 * (total - free) / total)}


def _meminfo():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                out[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    total = out.get("MemTotal", 0)
    if not total:
        return None
    available = out.get("MemAvailable", out.get("MemFree", 0))
    return {"total": total, "available": available, "used": total - available,
            "percent": round(100.0 * (total - available) / total),
            "swap_total": out.get("SwapTotal", 0),
            "swap_used": out.get("SwapTotal", 0) - out.get("SwapFree", 0)}


def _uptime():
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return 0


# Deliberately only services that SHOULD be running, so a red dot always means
# something is wrong. biscuit-mic-pump is not on this list: it is in no runlevel
# on this image - the assistant captures through its own
# --audio-input-command chain - so listing it showed a permanent "Stopped"
# against "Microphone" on a device whose microphones were working perfectly.
SERVICES = [
    ("biscuit-voice-assistant", "Voice assistant"),
    ("biscuit-dsp", "Audio DSP"),
    ("biscuit-audio", "Audio control"),
    ("biscuit-mic-coeff", "Microphone calibration"),
    ("biscuit-ring", "Light ring"),
    ("biscuit-va-leds", "Ring animations"),
    ("biscuit-pipewire", "Sound server"),
    ("bluetooth", "Bluetooth"),
    ("biscuit-btaudio", "Bluetooth audio"),
    ("biscuit-settings", "Settings page"),
    ("chronyd", "Clock"),
]


OPENRC_RUN = "/run/openrc"


def _daemon_alive(svc):
    """False only when OpenRC's record names a pid that is no longer there -
    a started service whose daemon died, which rc-status calls crashed."""
    try:
        with open(os.path.join(OPENRC_RUN, "daemons", svc, "001")) as f:
            rec = dict(line.rstrip("\n").partition("=")[::2] for line in f if "=" in line)
    except OSError:
        return True
    pidfile = rec.get("pidfile")
    if not pidfile:
        return True
    try:
        with open(pidfile) as f:
            pid = int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    return os.path.exists("/proc/%d" % pid)


def service_states():
    """Which of the device's own services are up.

    Read from OpenRC's own run directory - the started markers and the daemon
    records - rather than by running `rc-status --all`, which cost the About
    page an eighth of a second on every load. A started service whose daemon
    is gone counts as down, as rc-status would report it.
    """
    try:
        started = set(os.listdir(os.path.join(OPENRC_RUN, "started")))
    except OSError:
        return []
    return [{"name": n, "label": l, "running": n in started and _daemon_alive(n)}
            for n, l in SERVICES]


def system_info():
    return {
        "device_name": device_name(),
        "os": _os_release(),
        "kernel": os.uname().release,
        "device_pkg": _apk_version("device-amazon-biscuit"),
        "kernel_pkg": _apk_version("linux-amazon-biscuit"),
        "uptime": _uptime(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "memory": _meminfo(),
        "storage": [d for d in (_disk("/"), _disk("/boot"), _disk("/opt")) if d],
        "model": _model(),
    }


DEVICEINFO = "/usr/share/deviceinfo/deviceinfo"


def _model():
    """The device's own name, from the file the port already ships.

    Read rather than hard-coded: this was written as "3rd gen" from memory and
    the hardware is a 2nd gen, which is exactly the kind of thing a second copy
    of a fact gets wrong.
    """
    try:
        with open(DEVICEINFO) as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == "deviceinfo_name":
                    return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return "Amazon Echo Dot"


def _os_release():
    try:
        with open("/etc/os-release") as f:
            data = _kv_block(f.read())
        return data.get("PRETTY_NAME", "").strip('"')
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Optional packages
# ---------------------------------------------------------------------------
#
# The device ships core only: it boots, joins Wi-Fi, pairs Bluetooth, plays and
# records audio, and serves this page. The voice assistant and the network
# speaker are applications, and applications are a choice.
#
# THE MANIFEST IS SHARED WITH THE SETUP PORTAL, deliberately. biscuit-portal.py
# parses the same file to build its first-boot checkboxes, so an entry added for
# one surface appears on the other with no second list to keep in step.

APPS_FILE = "/usr/share/biscuit/apps.json"
PKG_RUNDIR = "/run/biscuit-packages"
# Every job's status, which is also the lock: nothing new starts while it says
# running. The app record is the last install or removal alone, so a later
# update check cannot overwrite its outcome - see biscuit-pkgjob.sh.
PKG_STATUS = os.path.join(PKG_RUNDIR, "status")
PKG_APPS_STATUS = os.path.join(PKG_RUNDIR, "apps-status")
PKG_JOB = "/usr/bin/biscuit-pkgjob.sh"


def available_packages():
    """The optional apps, each with whether it is installed.

    From apps.json, which the setup portal reads too. It replaced a
    pipe-separated setup-packages.conf that two parsers had to agree on - and
    once did not: a fifth field added for the portal made this parser reject
    every line, and the Apps page silently showed nothing (r141).
    """
    out = []
    try:
        with open(APPS_FILE, encoding="utf-8") as f:
            apps = json.load(f).get("apps", [])
    except (OSError, ValueError) as err:
        _log("cannot read %s: %s", APPS_FILE, err)
        return out
    for app in apps:
        names = app.get("packages") or []
        if not app.get("id") or not names:
            continue
        out.append({
            "id": app["id"],
            "label": app.get("label", app["id"]),
            "description": app.get("summary", ""),
            "packages": names,
            "installed": all(_apk_installed(n) for n in names),
        })
    return out


def _apk_installed(name):
    rc, _out, _err = _run(["apk", "info", "-e", name], timeout=10)
    return rc == 0


def package_job(path=None):
    """The running or last-finished package job, or None.

    Read from the status file biscuit-pkgjob.sh writes, rather than by looking
    for an apk process: a job that died leaves no process but must still be
    reportable, and 'finished with an error' is the state most worth showing.
    PKG_STATUS is every job's; PKG_APPS_STATUS is the last app install or
    removal.

    `id` changes with every write - the job renames a new file into place each
    time - so a reader can tell a record it has already acted on from a new one
    with the same words. `time` is the device's clock when it was written,
    which reads 2010 until the clock is set; `reason` is the job's one-word
    cause of a failure (see biscuit-pkgjob.sh). A status written before r295
    has neither, and reads as empty.
    """
    try:
        with open(path or PKG_STATUS) as f:
            st = os.fstat(f.fileno())
            job = _kv_block(f.read())
    except OSError:
        return None
    if not job.get("state"):
        return None
    when = job.get("time", "")
    return {
        "action": job.get("action", ""),
        "packages": job.get("packages", "").split(),
        "state": job["state"],                       # running | ok | failed
        "message": job.get("message", ""),
        "finished": job.get("finished") == "1",
        "reason": job.get("reason", ""),
        "origin": job.get("origin", ""),
        "time": int(when) if when.isdigit() else None,
        "id": "%d-%d" % (st.st_ino, st.st_mtime_ns),
    }


def start_package_job(action, pkg_id):
    """Install or remove one manifest entry, detached.

    Returns (ok, message). The caller polls package_job() for the outcome -
    installing the voice bundle is about 100 MB and takes minutes, which no
    browser will wait for.
    """
    if action not in ("add", "del"):
        return False, "Unknown action."
    entry = next((p for p in available_packages() if p["id"] == pkg_id), None)
    if entry is None:
        return False, "No such package."

    running = package_job()
    if running and running["state"] == "running":
        return False, "Another package operation is still running."

    if action == "add" and entry["installed"]:
        return True, "%s is already installed." % entry["label"]
    if action == "del" and not entry["installed"]:
        return True, "%s is not installed." % entry["label"]

    try:
        subprocess.Popen([PKG_JOB, action] + entry["packages"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as err:
        return False, "Could not start the package job: %s" % err
    verb = "Installing" if action == "add" else "Removing"
    return True, "%s %s. This can take a few minutes." % (verb, entry["label"])


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------
#
# WHY THIS IS ON THE SETTINGS PAGE AT ALL.
#
# A flash replaces the rootfs, so before biscuit-persist learned to keep them,
# both halves of SSH went with it: the host keys - every reconnect then failed
# with REMOTE HOST IDENTIFICATION HAS CHANGED - and ~/.ssh/authorized_keys. The
# only way back in was a password, which OpenSSH cannot supply
# non-interactively. Persistence stops the recurrence; this page is what lets
# someone install their key in the FIRST place, during setup, without ever
# needing a password login at all.
#
# WHAT IS DELIBERATELY NOT OFFERED: uploading a PRIVATE key, and editing
# sshd_config freehand. Both are ways to make a device unreachable or unsafe
# from a browser, and neither has a use here.

SSHD_CONFIG = "/etc/ssh/sshd_config"


def _ssh_paths():
    """The account whose authorized_keys this page manages, and where it lives."""
    entry = primary_account()
    if entry is None:
        return None, None
    return entry.pw_name, os.path.join(entry.pw_dir, ".ssh", "authorized_keys")


def _sshd_directive(key, default=""):
    """Read one sshd_config directive.

    FIRST occurrence wins, because that is what sshd itself does for most
    keywords. Reporting the last would disagree with the running daemon on any
    file that carries the keyword twice - which is exactly the file this page
    is most likely to be asked about.
    """
    try:
        with open(SSHD_CONFIG) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0].lower() == key.lower():
                    return parts[1].strip()
    except OSError:
        pass
    return default


def _key_fingerprints(path):
    """Fingerprint every key in a file, in file order.

    ssh-keygen does the parsing rather than this module, so what the page shows
    is what OpenSSH itself would say - and a line OpenSSH cannot read is simply
    not listed, instead of being displayed as though it were a working key.
    """
    out = []
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for idx, line in enumerate(lines):
        if not line.strip() or line.strip().startswith("#"):
            continue
        rc, so, _se = _run(["ssh-keygen", "-l", "-f", "/dev/stdin"],
                           timeout=6, stdin=line + chr(10))
        if rc != 0 or not so.strip():
            continue
        # "256 SHA256:abc... comment (ED25519)"
        fields = so.strip().split(None, 2)
        rest = fields[2] if len(fields) > 2 else ""
        ktype = ""
        if rest.endswith(")") and "(" in rest:
            ktype = rest[rest.rindex("(") + 1:-1]
            rest = rest[:rest.rindex("(")].strip()
        out.append({
            "index": idx,
            "bits": fields[0] if fields else "",
            "fingerprint": fields[1] if len(fields) > 1 else "",
            "comment": "" if rest == "no comment" else rest,
            "type": ktype,
        })
    return out


def ssh_state():
    username, auth = _ssh_paths()
    host_fp = ""
    rc, so, _se = _run(["ssh-keygen", "-l", "-f",
                        "/etc/ssh/ssh_host_ed25519_key.pub"], timeout=6)
    if rc == 0 and len(so.strip().split()) > 1:
        host_fp = so.strip().split()[1]
    rc, _so, _se = _run(["rc-service", "sshd", "status"], timeout=8)
    pw = _sshd_directive("PasswordAuthentication", "yes").lower()
    # Persistence is a property of the store, not of this file. A device whose
    # store failed to mount still runs SSH perfectly well, and the page must
    # not promise that the keys will survive the next flash when they will not.
    persisted = False
    if auth:
        try:
            persisted = os.path.ismount(os.path.dirname(auth))
        except OSError:
            persisted = False
    return {
        "available": os.path.exists(SSHD_CONFIG),
        "running": rc == 0,
        "in_runlevel": os.path.exists("/etc/runlevels/default/sshd"),
        "username": username or "",
        "persisted": persisted,
        "password_auth": pw not in ("no", "false", "off"),
        "host_fingerprint": host_fp,
        "keys": _key_fingerprints(auth),
    }


def add_ssh_key(text):
    """Append one public key, after OpenSSH agrees that it is one.

    Only a bare `type base64 [comment]` line is accepted. authorized_keys also
    supports a leading options field - `command=`, `environment=` and friends -
    which turns a key into arbitrary code execution on connect. Nothing on this
    page needs that, so a line carrying options is refused rather than stored.
    """
    text = (text or "").strip()
    if not text:
        return False, "Paste a public key first."
    if chr(10) in text or chr(13) in text:
        return False, "Paste one key, on a single line."
    if "PRIVATE KEY" in text.upper():
        return False, "That is a PRIVATE key. Paste the .pub file instead."
    if not text.startswith(("ssh-", "ecdsa-", "sk-")):
        return False, ("That does not look like a public key. It should start "
                       "with ssh-ed25519, ssh-rsa or similar, and it must be "
                       "the .pub file.")

    rc, so, _se = _run(["ssh-keygen", "-l", "-f", "/dev/stdin"],
                       timeout=6, stdin=text + chr(10))
    if rc != 0:
        return False, "OpenSSH does not recognise that as a public key."
    parts = so.strip().split()
    fp = parts[1] if len(parts) > 1 else ""

    username, auth = _ssh_paths()
    if not auth:
        return False, "There is no account on this device to add a key to."
    for existing in _key_fingerprints(auth):
        if existing["fingerprint"] == fp:
            return True, "That key is already installed."

    entry = primary_account()
    try:
        d = os.path.dirname(auth)
        os.makedirs(d, exist_ok=True)
        with open(auth, "a") as fh:
            fh.write(text + chr(10))
        # sshd's StrictModes ignores an authorized_keys the user does not own,
        # or a .ssh anyone else can write to - silently, which reads as "the
        # key did not work" rather than as a permissions problem.
        os.chmod(d, 0o700)
        os.chmod(auth, 0o600)
        if entry is not None:
            os.chown(d, entry.pw_uid, entry.pw_gid)
            os.chown(auth, entry.pw_uid, entry.pw_gid)
    except OSError as err:
        return False, "Could not write authorized_keys: %s" % err
    return True, "Key added for %s." % username


def remove_ssh_key(fingerprint):
    """Drop one key by fingerprint, refusing when it is the last way in."""
    _username, auth = _ssh_paths()
    if not auth or not os.path.exists(auth):
        return False, "No keys are installed."
    keys = _key_fingerprints(auth)
    match = [k for k in keys if k["fingerprint"] == fingerprint]
    if not match:
        return False, "No such key."
    if len(keys) == 1 and not ssh_state()["password_auth"]:
        return False, ("That is the only key and password sign-in is off, so "
                       "removing it would lock you out. Turn password sign-in "
                       "back on first, or add another key.")
    drop = {k["index"] for k in match}
    try:
        with open(auth) as fh:
            lines = fh.read().splitlines()
        with open(auth, "w") as fh:
            for idx, line in enumerate(lines):
                if idx not in drop:
                    fh.write(line + chr(10))
        os.chmod(auth, 0o600)
    except OSError as err:
        return False, "Could not rewrite authorized_keys: %s" % err
    return True, "Key removed."


def _sshd_binary():
    """Whichever SSH daemon this image actually installs.

    Alpine ships the plain server as /usr/sbin/sshd and the PAM build as
    /usr/sbin/sshd.pam - and this device has the PAM one, so the obvious
    hard-coded "sshd" resolves to nothing. The first version of this code did
    exactly that and reported "sshd rejected the new configuration" about a
    config sshd had never been shown, having already written it. A validator
    that cannot run must not be reported as a rejection.
    """
    for cand in ("/usr/sbin/sshd.pam", "/usr/sbin/sshd", "/usr/bin/sshd"):
        if os.path.exists(cand):
            return cand
    return shutil.which("sshd.pam") or shutil.which("sshd")


def set_ssh_password_auth(on):
    """Turn password sign-in on or off, with a lockout guard on the way off."""
    on = bool(on)
    if not on:
        _username, auth = _ssh_paths()
        if not _key_fingerprints(auth):
            return False, ("Add a public key first. Turning password sign-in "
                           "off with no key installed would lock you out of "
                           "this device permanently.")
    want = "yes" if on else "no"
    try:
        with open(SSHD_CONFIG) as fh:
            lines = fh.read().splitlines()
    except OSError as err:
        return False, "Could not read sshd_config: %s" % err

    out, seen = [], False
    for line in lines:
        stripped = line.strip()
        bare = stripped[1:].strip() if stripped.startswith("#") else stripped
        parts = bare.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "passwordauthentication":
            if not seen:
                out.append("PasswordAuthentication " + want)
                seen = True
            # Later duplicates are dropped rather than kept: a second,
            # contradicting line is how a setting comes to look ignored.
            continue
        out.append(line)
    if not seen:
        out.append("PasswordAuthentication " + want)

    original = chr(10).join(lines) + chr(10)
    try:
        tmp = SSHD_CONFIG + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(chr(10).join(out) + chr(10))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        _durable_replace(tmp, SSHD_CONFIG)
    except OSError as err:
        return False, "Could not write sshd_config: %s" % err

    # Validate BEFORE reloading. A config sshd rejects makes it refuse to
    # start, and on a headless device that is discovered by not being able to
    # log in - the exact failure this whole section exists to prevent. On a
    # rejection the previous file is PUT BACK: leaving a config the daemon will
    # not accept, and merely reporting that, is how the next restart bricks
    # remote access.
    sshd = _sshd_binary()
    if sshd:
        rc, _so, se = _run([sshd, "-t"], timeout=8)
        if rc != 0:
            try:
                with open(SSHD_CONFIG, "w") as fh:
                    fh.write(original)
            except OSError:
                pass
            return False, ("sshd rejected the new configuration, so nothing "
                           "was changed: %s" % se.strip()[:140])
    else:
        _log("no sshd binary found; wrote sshd_config without validating")
    _run(["rc-service", "sshd", "reload"], timeout=15)
    return True, ("Password sign-in enabled." if on
                  else "Password sign-in disabled. Keys only from now on.")


def set_sshd_enabled(on):
    """Start or stop SSH, keeping the runlevel in step so a reboot agrees."""
    on = bool(on)
    if on:
        _run(["rc-update", "add", "sshd", "default"], timeout=15)
        rc, _so, se = _run(["rc-service", "sshd", "start"], timeout=25)
        if rc != 0:
            return False, "Could not start SSH: %s" % se.strip()[:160]
        return True, "SSH is on."
    _run(["rc-update", "del", "sshd", "default"], timeout=15)
    _run(["rc-service", "sshd", "stop"], timeout=25)
    return True, "SSH is off, and will stay off after a reboot."
