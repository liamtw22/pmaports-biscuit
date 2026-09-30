#!/usr/bin/env python3
"""Passive Bluetooth LE proxy for Home Assistant, speaking the ESPHome API.

Home Assistant already talks to this device over the ESPHome native API, which
is the same protocol ESPHome Bluetooth Proxies use - so forwarding BLE
advertisements needs no new integration, no MQTT and no custom component. HA
discovers a second ESPHome device and starts feeding its Bluetooth stack.

WHY THE ADVERTISEMENTS COME FROM AN HCI MONITOR SOCKET

BlueZ's D-Bus API exposes only a *parsed* view - ServiceData by UUID,
ManufacturerData by company id - which loses the original AD ordering and any
type BlueZ does not itself understand. The HCI monitor channel is what btmon
uses: passive, coexists with bluetoothd, and yields the RAW advertisement
bytes, which is what Home Assistant prefers to parse itself.

It is also plain `socket` from the standard library, so this service needs no
BlueZ binding and no aioesphomeapi. The only dependency is py3-protobuf
(1.3 MB) plus two generated _pb2 files vendored beside this script. That is
what lets the proxy live in the CORE package rather than behind the ~100 MB
voice bundle - a Bluetooth proxy is useful on a device that never runs the
voice assistant.

WHY IT DUTY-CYCLES INSTEAD OF SCANNING CONTINUOUSLY

Measured on this hardware, on 2.4 GHz Wi-Fi (channel 1, -56 dBm):

                        idle          scanning
    download            0.38/0.35 s   0.77/0.65 s      (~2x slower)
    ping rtt avg        6.7/6.3 ms    6.8/6.6 ms       (unchanged)
    ping rtt max        10.9/8.7 ms   36.9/38.6 ms     (3-4x worse)

Continuous scanning roughly HALVES 2.4 GHz Wi-Fi throughput. Median latency is
untouched, so this is airtime contention rather than congestion - the radio is
shared and BLE takes its half. On a device whose main job is voice over Wi-Fi
that is a bad permanent trade, so the scanner runs in a window/idle cycle.
A 10 s window every 60 s costs roughly a sixth of the airtime while still
refreshing every sensor well inside the timeouts Home Assistant uses.

A2DP is NOT a reason to duty-cycle: 25 s of tone with a continuous scan running
produced 0 underruns and sounded clean. Only Wi-Fi pays.
"""

import logging
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import active  # noqa: E402
from aioesphomeapi_min import api_pb2  # noqa: E402
from aioesphomeapi_min.api_pb2 import (  # noqa: E402
    BluetoothLERawAdvertisement,
    BluetoothLERawAdvertisementsResponse,
    AuthenticationResponse,
    DeviceInfoResponse,
    DisconnectRequest,
    DisconnectResponse,
    HelloRequest,
    HelloResponse,
    ListEntitiesDoneResponse,
    PingRequest,
    PingResponse,
)

_LOGGER = logging.getLogger("biscuit-btproxy")

CONFIG = "/opt/persist/btproxy.env"
STATE_DIR = "/run/biscuit-btproxy"
STATE = os.path.join(STATE_DIR, "state")

# Bluetooth proxy feature flags, from the ESPHome API.
#   PASSIVE_SCAN       = 1 << 0   forward advertisements
#   ACTIVE_CONNECTIONS = 1 << 1   let HA open GATT connections THROUGH us
#   RAW_ADVERTISEMENTS = 1 << 5   send raw AD bytes rather than parsed fields
#
# ACTIVE_CONNECTIONS is set only when active.py loads and BTPROXY_ACTIVE is on.
# It was left off at first because it needs the whole GATT message set mapped
# onto BlueZ plus connection-slot management, and this controller's firmware has
# a documented habit of asserting. Passive remains the 80% case (BTHome,
# Xiaomi/ATC, Govee, Victron, iBeacon all broadcast) and cannot wedge anything,
# so active stays a switch rather than a hard-wired capability - and the
# advertised flag follows the switch, because claiming a capability we are not
# serving makes Home Assistant queue connections that never complete.
FEATURE_PASSIVE_SCAN = 1 << 0
FEATURE_ACTIVE_CONNECTIONS = 1 << 1
FEATURE_RAW_ADVERTISEMENTS = 1 << 5

# Shared connect exclusion with the :8080 settings portal. Mirrored in
# biscuit-netcfg.py, which documents the measurements behind the contract: this
# covers CONNECTS ONLY because concurrent LE discovery was measured NOT to
# conflict on this controller (~26 adv/s alone vs ~21 adv/s with two independent
# BlueZ discovery clients, and stopping one left the other running). The
# apparent conflict in the first test was BlueZ interleaving BR/EDR inquiry
# under an unfiltered `scan on`, which blanks LE reception for ~10 s in every
# ~16 s. This scanner uses `scan le`, so it never causes that itself.
BT_BUSY_DIR = "/run/biscuit-bt/busy"
BT_BUSY_OWNER = "btproxy"

# Never cached as an identity - see ProxyServer.adapter_mac.
MAC_UNKNOWN = "00:00:00:00:00:00"

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_CHANNEL_MONITOR = 2
HCI_DEV_NONE = 0xFFFF

MONITOR_OP_EVENT = 0x0003
HCI_EV_LE_META = 0x3E
LE_SUB_ADV_REPORT = 0x02
LE_SUB_EXT_ADV_REPORT = 0x0D


def resolve_bind(cfg):
    """The address to listen on.

    "auto" means the configured interface's own IPv4. If that cannot be read -
    the interface is down, renaming, or has no address yet - fall back to
    0.0.0.0 and SAY SO, because silently listening everywhere is exactly the
    behaviour this setting exists to stop.
    """
    want = str(cfg.get("bind", "auto")).strip()
    if want and want != "auto":
        return want
    iface = str(cfg.get("interface", "wlan0")).strip() or "wlan0"
    try:
        import fcntl
        import struct
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # SIOCGIFADDR
            packed = fcntl.ioctl(s.fileno(), 0x8915,
                                 struct.pack("256s", iface.encode()[:15]))
            return socket.inet_ntoa(packed[20:24])
        finally:
            s.close()
    except OSError as err:
        _LOGGER.warning("cannot read %s address (%s); listening on 0.0.0.0, "
                        "which exposes the proxy on every interface", iface, err)
        return "0.0.0.0"


def load_config():
    """Settings, with defaults chosen from the airtime measurements above."""
    cfg = {
        "enabled": False,
        "scan_secs": 10,
        "idle_secs": 50,
        "port": 6054,
        # Which address to listen on. "auto" resolves the configured network
        # interface's own address, which is what linux_voice_assistant already
        # does for its API on 6053.
        #
        # This used to be a hard-coded 0.0.0.0, which also exposed the proxy on
        # the USB gadget network, on the setup hotspot, and on anything else
        # that ever comes up - for a service that relays the owner's home BLE
        # advertisements and has no authentication of its own. Binding the LAN
        # interface does not authenticate it, but it stops it appearing on
        # networks nobody chose to put it on.
        "bind": "auto",
        "interface": "wlan0",
        "name": "biscuit-btproxy",
        # Active is opt-in on top of the proxy being on. It holds connections,
        # which suspends scanning entirely, and this controller's connection
        # ceiling is unmeasured - so it should be a decision, not a default.
        "active": False,
        "max_connections": active.DEFAULT_LIMIT,
    }
    try:
        with open(CONFIG) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().lower().replace("btproxy_", "")
                v = v.strip().strip('"').strip("'")
                if k in ("bind", "interface"):
                    if v:
                        cfg[k] = v
                elif k in ("scan_secs", "idle_secs", "port", "max_connections"):
                    try:
                        cfg[k] = max(1, int(v))
                    except ValueError:
                        pass
                elif k in ("enabled", "active"):
                    cfg[k] = v.lower() in ("1", "yes", "true", "on")
                elif k == "name":
                    if v:
                        cfg["name"] = v
    except OSError:
        pass
    return cfg


# ---------------------------------------------------------------- framing ---
#
# ESPHome plaintext frame: 0x00, varint payload length, varint message type,
# payload. There is an encrypted (Noise) variant too, but the assistant on this
# device already serves the API in plaintext and Home Assistant accepts it, so
# there is nothing to gain from carrying a crypto dependency here.


def _varint(value):
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(buf, pos):
    """Return (value, new_pos), or (None, pos) if the buffer is short."""
    result = shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")
    return None, pos


def encode_frame(msg_type, payload):
    return bytes([0]) + _varint(len(payload)) + _varint(msg_type) + payload


# Message type ids from the ESPHome API. Hard-coded rather than imported from
# aioesphomeapi.core so this service does not need that package at all; they are
# protocol constants and do not move.
MT_HELLO_REQUEST = 1
MT_HELLO_RESPONSE = 2
# Types 3/4 are named Authentication* in the generated protobuf, though the
# ESPHome docs call this the connect phase.
MT_CONNECT_REQUEST = 3
MT_CONNECT_RESPONSE = 4
MT_DISCONNECT_REQUEST = 5
MT_DISCONNECT_RESPONSE = 6
MT_PING_REQUEST = 7
MT_PING_RESPONSE = 8
MT_DEVICE_INFO_REQUEST = 9
MT_DEVICE_INFO_RESPONSE = 10
MT_LIST_ENTITIES_REQUEST = 11
MT_LIST_ENTITIES_DONE_RESPONSE = 19
MT_SUBSCRIBE_STATES_REQUEST = 20
MT_SUBSCRIBE_BLUETOOTH_LE_ADVERTISEMENTS = 66
MT_BLUETOOTH_LE_RAW_ADVERTISEMENTS_RESPONSE = 93
MT_UNSUBSCRIBE_BLUETOOTH_LE_ADVERTISEMENTS = 87


# ------------------------------------------------------- advertisements -----


def parse_le_adv_reports(body):
    """Yield (address_int, address_type, rssi, ad_bytes) from an LE Meta event.

    Layout after the 0x3E/len/subevent/num_reports header, per report:
        event_type(1) addr_type(1) addr(6, little-endian) data_len(1)
        data(data_len) rssi(1, signed)

    The HCI address is little-endian; Home Assistant wants the MAC as a big
    -endian integer, so it is reversed here rather than in the encoder.
    """
    if len(body) < 4 or body[0] != HCI_EV_LE_META:
        return
    if body[2] != LE_SUB_ADV_REPORT:
        # Extended reports (0x0D) have a different layout. This controller is
        # BT 4.0 and does not emit them; ignoring is safer than mis-parsing.
        return
    num = body[3]
    pos = 4
    for _ in range(num):
        if pos + 9 > len(body):
            return
        addr_type = body[pos + 1]
        addr = body[pos + 2:pos + 8]
        dlen = body[pos + 8]
        pos += 9
        if pos + dlen + 1 > len(body):
            return
        data = bytes(body[pos:pos + dlen])
        pos += dlen
        rssi = struct.unpack_from("<b", body, pos)[0]
        pos += 1
        addr_int = int.from_bytes(bytes(reversed(addr)), "big")
        yield addr_int, addr_type, rssi, data


class MonitorReader(threading.Thread):
    """Read raw LE advertising reports off the HCI monitor channel.

    Passive: it observes what the controller already reports and never sends a
    command, so it cannot disturb bluetoothd, an A2DP stream or a pairing
    session. Advertisements are dropped when nobody is subscribed, so an
    unsubscribed proxy costs nothing but a blocked recv.
    """

    daemon = True

    def __init__(self):
        super().__init__(name="hci-monitor")
        self.q = queue.Queue(maxsize=2000)
        self.subscribed = threading.Event()
        self.seen = 0
        self.dropped = 0
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC,
                              BTPROTO_HCI)
            s.bind((HCI_DEV_NONE, HCI_CHANNEL_MONITOR))
        except OSError as err:
            _LOGGER.error("cannot open the HCI monitor socket: %s", err)
            return
        _LOGGER.info("HCI monitor open")
        s.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data = s.recv(4096)
            except socket.timeout:
                continue
            except OSError as err:
                _LOGGER.error("monitor read failed: %s", err)
                return
            if len(data) < 6:
                continue
            opcode, _index, plen = struct.unpack_from("<HHH", data, 0)
            if opcode != MONITOR_OP_EVENT:
                continue
            body = data[6:6 + plen]
            for adv in parse_le_adv_reports(body):
                self.seen += 1
                if not self.subscribed.is_set():
                    continue
                try:
                    self.q.put_nowait(adv)
                except queue.Full:
                    # A stalled client must not grow this without bound. Losing
                    # an advertisement is harmless - they repeat.
                    self.dropped += 1


def bt_busy_holders(ignore=None):
    """Owners currently holding the connect flag, skipping dead ones.

    A flag file is named after its owner and holds that owner's PID, so a holder
    that crashed mid-connect does not silence the scanner forever.
    """
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
                pid = int(fh.read().strip() or "0")
        except (OSError, ValueError):
            pid = 0
        if pid > 0 and not os.path.exists("/proc/%d" % pid):
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        held.append(name)
    return held


def bt_busy_set(held):
    """Publish or clear this process's connect flag."""
    path = os.path.join(BT_BUSY_DIR, BT_BUSY_OWNER)
    try:
        if held:
            os.makedirs(BT_BUSY_DIR, exist_ok=True)
            with open(path, "w") as fh:
                fh.write(str(os.getpid()))
        else:
            os.unlink(path)
    except OSError:
        pass


class Scanner(threading.Thread):
    """Drive LE discovery in a window/idle cycle.

    bluetoothctl is used rather than BlueZ over D-Bus because it is already
    installed, needs no binding, and this is the one place where being a
    subprocess costs nothing - it runs once per cycle, not per advertisement.

    Discovery is what makes the controller report advertisements; the monitor
    socket only observes. Some advertisements do arrive with no discovery
    running, because bluetoothd keeps its own passive scan for reconnects - but
    that is not something to rely on, hence the explicit cycle.
    """

    daemon = True

    def __init__(self, scan_secs, idle_secs, monitor):
        super().__init__(name="scanner")
        self.scan_secs = scan_secs
        self.idle_secs = idle_secs
        self.monitor = monitor
        self.cycles = 0
        self._stop = threading.Event()
        self._paused = threading.Event()

    def stop(self):
        self._stop.set()

    def set_paused(self, paused):
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def run(self):
        while not self._stop.is_set():
            # Suspended while a GATT connection is open - see
            # ProxyServer.set_scan_paused.
            if self._paused.is_set():
                self._stop.wait(1.0)
                continue
            # The settings portal is pairing or connecting. Discovery raised
            # underneath a connection attempt is what produces
            # le-connection-abort-by-local, so stay off the air until it is
            # done. See bt_busy_holders for why this covers connects only.
            if bt_busy_holders(ignore=BT_BUSY_OWNER):
                self._stop.wait(1.0)
                continue
            # Only burn airtime when Home Assistant is actually listening.
            if not self.monitor.subscribed.is_set():
                self._stop.wait(2.0)
                continue
            self.cycles += 1
            try:
                subprocess.run(
                    ["bluetoothctl", "--timeout", str(self.scan_secs), "scan", "le"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=self.scan_secs + 15, check=False)
            except (OSError, subprocess.TimeoutExpired) as err:
                _LOGGER.warning("scan cycle failed: %s", err)
            self._stop.wait(self.idle_secs)


def publish_state(cfg, monitor, scanner, clients, activep=None):
    """A small status file, the shape the ring and volume services already use."""
    nl = chr(10)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            f.write("enabled=%s%s" % ("1" if cfg["enabled"] else "0", nl))
            f.write("scan_secs=%d%s" % (cfg["scan_secs"], nl))
            f.write("idle_secs=%d%s" % (cfg["idle_secs"], nl))
            f.write("port=%d%s" % (cfg["port"], nl))
            f.write("bind=%s%s" % (cfg.get("bind", "auto"), nl))
            f.write("interface=%s%s" % (cfg.get("interface", "wlan0"), nl))
            f.write("clients=%d%s" % (clients, nl))
            f.write("subscribed=%s%s" % ("1" if monitor.subscribed.is_set() else "0", nl))
            f.write("adverts_seen=%d%s" % (monitor.seen, nl))
            f.write("adverts_dropped=%d%s" % (monitor.dropped, nl))
            f.write("scan_cycles=%d%s" % (scanner.cycles if scanner else 0, nl))
            # The settings page and the Home Assistant switch both read their
            # state from here rather than from the config, so that what they
            # show is what the service is doing, not what it was last asked.
            f.write("active=%s%s" % ("1" if activep is not None else "0", nl))
            f.write("connections=%d%s"
                    % (len(activep.connected) if activep is not None else 0, nl))
            f.write("max_connections=%d%s"
                    % (activep.limit if activep is not None else 0, nl))
        os.rename(tmp, STATE)
    except OSError:
        pass


class ProxyConnection(threading.Thread):
    """One Home Assistant connection."""

    daemon = True

    def __init__(self, sock, addr, cfg, monitor, server):
        super().__init__(name="conn")
        self.sock = sock
        self.addr = addr
        self.cfg = cfg
        self.monitor = monitor
        self.server = server
        self.subscribed = False
        self.buf = bytearray()

    def send(self, msg_type, msg):
        self.sock.sendall(encode_frame(msg_type, msg.SerializeToString()))

    def device_info(self):
        info = DeviceInfoResponse()
        info.name = self.cfg["name"]
        info.friendly_name = self.cfg["name"]
        info.mac_address = self.server.adapter_mac()
        info.esphome_version = "biscuit-btproxy"
        info.model = "Amazon Echo Dot (2016)"
        info.manufacturer = "postmarketOS"
        flags = FEATURE_PASSIVE_SCAN | FEATURE_RAW_ADVERTISEMENTS
        if self.cfg.get("active"):
            flags |= FEATURE_ACTIVE_CONNECTIONS
        info.bluetooth_proxy_feature_flags = flags
        return info

    def run(self):
        _LOGGER.info("client connected: %s", self.addr)
        try:
            self.sock.settimeout(1.0)
            threading.Thread(target=self.pump_advertisements, daemon=True).start()
            while True:
                try:
                    data = self.sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                self.buf += data
                if not self.drain():
                    break
        except OSError as err:
            _LOGGER.debug("client %s: %s", self.addr, err)
        finally:
            _LOGGER.info("client disconnected: %s", self.addr)
            self.subscribed = False
            try:
                self.sock.close()
            except OSError:
                pass
            if self.server.active is not None:
                self.server.active.client_gone(self)
            self.server.drop(self)

    def drain(self):
        """Parse whole frames out of the buffer. False means close the link."""
        while True:
            if len(self.buf) < 3:
                return True
            if self.buf[0] != 0:
                _LOGGER.warning("frame desync from %s; closing", self.addr)
                return False
            length, pos = _read_varint(self.buf, 1)
            if length is None:
                return True
            msg_type, pos = _read_varint(self.buf, pos)
            if msg_type is None:
                return True
            if len(self.buf) < pos + length:
                return True
            payload = bytes(self.buf[pos:pos + length])
            del self.buf[:pos + length]
            if not self.handle(msg_type, payload):
                return False

    def handle(self, msg_type, payload):
        if msg_type == MT_HELLO_REQUEST:
            req = HelloRequest.FromString(payload)
            _LOGGER.debug("hello from %s", req.client_info)
            self.send(MT_HELLO_RESPONSE, HelloResponse(
                api_version_major=1, api_version_minor=10, name=self.cfg["name"]))
        elif msg_type == MT_CONNECT_REQUEST:
            self.send(MT_CONNECT_RESPONSE, AuthenticationResponse())
        elif msg_type == MT_DEVICE_INFO_REQUEST:
            self.send(MT_DEVICE_INFO_RESPONSE, self.device_info())
        elif msg_type == MT_LIST_ENTITIES_REQUEST:
            # A proxy exposes no entities of its own; HA still expects the done.
            self.send(MT_LIST_ENTITIES_DONE_RESPONSE, ListEntitiesDoneResponse())
        elif msg_type == MT_SUBSCRIBE_STATES_REQUEST:
            pass
        elif msg_type == MT_PING_REQUEST:
            self.send(MT_PING_RESPONSE, PingResponse())
        elif msg_type == MT_DISCONNECT_REQUEST:
            self.send(MT_DISCONNECT_RESPONSE, DisconnectResponse())
            return False
        elif msg_type == MT_SUBSCRIBE_BLUETOOTH_LE_ADVERTISEMENTS:
            _LOGGER.info("Home Assistant subscribed to advertisements")
            self.subscribed = True
            self.monitor.subscribed.set()
        elif msg_type == MT_UNSUBSCRIBE_BLUETOOTH_LE_ADVERTISEMENTS:
            _LOGGER.info("Home Assistant unsubscribed")
            self.subscribed = False
            self.server.refresh_subscription()
        elif self.server.active is not None:
            # Everything GATT-shaped belongs to the active module. It answers
            # False for anything it does not own, so an unknown message is
            # ignored here rather than mistaken for one of its own.
            return self.server.active.handle(self, msg_type, payload) or True
        return True

    def pump_advertisements(self):
        """Forward advertisements in batches.

        Batched because the field is repeated and a busy room produces hundreds
        of reports a second; one frame per advertisement would spend more time
        in syscalls than on the radio.
        """
        while True:
            if not self.subscribed:
                time.sleep(0.2)
                continue
            batch = []
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline and len(batch) < 32:
                try:
                    batch.append(self.monitor.q.get(timeout=0.1))
                except queue.Empty:
                    break
            if not batch:
                continue
            msg = BluetoothLERawAdvertisementsResponse()
            for addr, addr_type, rssi, data in batch:
                a = msg.advertisements.add()
                a.address = addr
                a.address_type = addr_type
                a.rssi = rssi
                a.data = data
            try:
                self.send(MT_BLUETOOTH_LE_RAW_ADVERTISEMENTS_RESPONSE, msg)
            except OSError:
                return


class ProxyServer:
    """Accept Home Assistant connections and hold the shared scanner state."""

    def __init__(self, cfg, monitor, scanner=None):
        self.cfg = cfg
        self.monitor = monitor
        self.scanner = scanner
        self.clients = []
        self.lock = threading.Lock()
        self._mac = None
        self.active = None
        if cfg.get("active"):
            # Imported lazily via the module so a passive-only device never
            # loads jeepney or opens a bus connection.
            self.active = active.ActiveProxy(
                api_pb2,
                limit=int(cfg.get("max_connections") or active.DEFAULT_LIMIT),
                on_scan_pause=self.set_scan_paused)
            _LOGGER.info("active connections enabled (limit %d)", self.active.limit)

    def set_scan_paused(self, paused):
        """Scanning and connecting cannot overlap on this radio.

        A connection attempt while the adapter is discovering fails every time
        with le-connection-abort-by-local. So the scanner is suspended for as
        long as any connection is open, and resumes when the last one closes -
        advertisements simply stop arriving for that period, which Home
        Assistant tolerates far better than connections that will not open.

        The same flag is published for the settings portal, so a user pressing
        "Scan" for a speaker while Home Assistant holds a GATT connection defers
        instead of tearing it down.
        """
        if self.scanner is not None:
            self.scanner.set_paused(paused)
        bt_busy_set(paused)

    def adapter_mac(self):
        """A stable unique id for Home Assistant.

        HA keys an ESPHome device on mac_address, so this MUST NOT be the same
        value the voice assistant reports or the two would collide as one
        device. The Bluetooth adapter address would be ideal and is genuinely
        distinct, but this kernel exposes no address file under
        /sys/class/bluetooth/hci0 - only device/power/reset/rfkill/subsystem -
        so it has to come from bluetoothctl, which needs bluetoothd up.

        The fallback derives an address from wlan0 with the locally-administered
        bit set. That is stable across reboots, cannot collide with the real
        wlan0 MAC the assistant uses, and is exactly what that bit is for.

        BOTH sources can be missing at boot. This service starts before
        bluetoothd answers and before the CONSYS driver has created wlan0, and
        the first run of this really did log 00:00:00:00:00:00 - which HA would
        then have used as the device's permanent identity, colliding with any
        other device that failed the same way. So the sentinel is never cached:
        a caller that got it asks again, and the answer is resolved for real as
        soon as either source appears.
        """
        if self._mac and self._mac != MAC_UNKNOWN:
            return self._mac
        mac = self._resolve_mac()
        if mac != MAC_UNKNOWN:
            self._mac = mac
        return mac

    def _resolve_mac(self):
        try:
            out = subprocess.run(["bluetoothctl", "show"], capture_output=True,
                                 text=True, timeout=8).stdout
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Controller "):
                    mac = line.split()[1].upper()
                    if len(mac) == 17:
                        return mac
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass
        try:
            with open("/sys/class/net/wlan0/address") as f:
                octets = f.read().strip().upper().split(":")
            octets[0] = "%02X" % (int(octets[0], 16) ^ 0x02)
            return ":".join(octets)
        except (OSError, ValueError, IndexError):
            return MAC_UNKNOWN

    def drop(self, conn):
        with self.lock:
            if conn in self.clients:
                self.clients.remove(conn)
        self.refresh_subscription()

    def refresh_subscription(self):
        """Stop scanning when the last subscriber goes away.

        This is what makes the airtime cost proportional: with nobody
        subscribed the scanner idles and Wi-Fi gets its throughput back.
        """
        with self.lock:
            any_sub = any(c.subscribed for c in self.clients)
        if any_sub:
            self.monitor.subscribed.set()
        else:
            self.monitor.subscribed.clear()

    def serve(self, scanner):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        host = resolve_bind(self.cfg)
        srv.bind((host, self.cfg["port"]))
        srv.listen(4)
        srv.settimeout(2.0)
        _LOGGER.info("listening on %s:%d as %s (%s)",
                     host, self.cfg["port"], self.cfg["name"], self.adapter_mac())
        last_state = 0.0
        while True:
            try:
                sock, addr = srv.accept()
            except socket.timeout:
                sock = None
            except OSError as err:
                _LOGGER.error("accept failed: %s", err)
                return
            if sock is not None:
                conn = ProxyConnection(sock, addr[0], self.cfg, self.monitor, self)
                with self.lock:
                    self.clients.append(conn)
                conn.start()
            now = time.monotonic()
            if now - last_state > 5.0:
                last_state = now
                with self.lock:
                    n = len(self.clients)
                publish_state(self.cfg, self.monitor, scanner, n, self.active)


# mDNS is published by a static avahi service file, /etc/avahi/services/
# biscuit-btproxy.service, whose port the init script keeps in step with the
# config. This image ships the avahi daemon but no avahi CLI tools, so there is
# nothing to spawn - and a file avoids supervising a subprocess whose only job
# is to sit there.


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()

    monitor = MonitorReader()
    if not cfg["enabled"]:
        # Stay running but inert, so enabling from :8080 or Home Assistant is a
        # service restart rather than a runlevel change, and the state file
        # still exists for the settings page to read.
        _LOGGER.info("disabled in %s; idling", CONFIG)
        publish_state(cfg, monitor, None, 0)
        while True:
            time.sleep(3600)

    monitor.start()
    scanner = Scanner(cfg["scan_secs"], cfg["idle_secs"], monitor)
    scanner.start()
    _LOGGER.info("passive proxy up: %ds scan every %ds",
                 cfg["scan_secs"], cfg["scan_secs"] + cfg["idle_secs"])
    try:
        ProxyServer(cfg, monitor, scanner).serve(scanner)
    finally:
        scanner.stop()
        monitor.stop()


if __name__ == "__main__":
    main()
