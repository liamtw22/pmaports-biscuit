"""BlueZ GATT client over D-Bus, for the active half of the Bluetooth proxy.

Installed as /usr/lib/biscuit-btproxy/bluez.py, beside the proxy that imports it.

WHY D-BUS AND NOT bluetoothctl

The passive half needs nothing but a raw socket, but active connections need
real GATT operations - read, write, subscribe - and bluetoothctl exposes those
only through an interactive menu. Driving that non-interactively is exactly the
kind of screen-scraping that has already cost this project a day: its scan
output was unparseable enough that a device advertising a name went undetected
until the raw advertising bytes were read instead.

jeepney is a pure-Python D-Bus client at 105 KiB, which keeps the proxy's total
dependency footprint small enough to stay in the core package.

HANDLES

Home Assistant addresses characteristics by GATT handle. BlueZ does not publish
handles as properties, but it encodes them in its object paths:

    /org/bluez/hci0/dev_XX_.../service00b9/char00ba/desc00bc

so the trailing hex is the handle. That is a documented BlueZ convention and
the only way to map ESPHome's handle-based protocol onto BlueZ's path-based
one without a second round trip per attribute.
"""

import logging
import re
import threading
import time

from jeepney import DBusAddress, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection

_LOGGER = logging.getLogger("biscuit-btproxy.bluez")

BLUEZ = "org.bluez"
ADAPTER = "/org/bluez/hci0"
I_DEVICE = "org.bluez.Device1"
I_SERVICE = "org.bluez.GattService1"
I_CHAR = "org.bluez.GattCharacteristic1"
I_DESC = "org.bluez.GattDescriptor1"
I_PROPS = "org.freedesktop.DBus.Properties"
I_OM = "org.freedesktop.DBus.ObjectManager"

_HANDLE_RE = re.compile(r"(?:service|char|desc)([0-9a-fA-F]{4})$")


def handle_of(path):
    """The GATT handle BlueZ encoded in an object path, or 0."""
    m = _HANDLE_RE.search(path)
    return int(m.group(1), 16) if m else 0


def addr_to_int(mac):
    return int(mac.replace(":", ""), 16)


def int_to_addr(value):
    b = value.to_bytes(6, "big")
    return ":".join("%02X" % x for x in b)


def uuid_to_pair(uuid_str):
    """A 128-bit UUID as the (msb, lsb) uint64 pair the ESPHome API uses."""
    h = uuid_str.replace("-", "")
    if len(h) == 4:            # 16-bit UUID, expand against the Bluetooth base
        h = "0000%s00001000800000805f9b34fb" % h
    elif len(h) == 8:
        h = "%s00001000800000805f9b34fb" % h
    v = int(h, 16)
    return [(v >> 64) & 0xFFFFFFFFFFFFFFFF, v & 0xFFFFFFFFFFFFFFFF]


class BlueZError(Exception):
    pass


class BlueZ:
    """Blocking BlueZ client.

    Blocking on purpose: every operation here is driven by a request from Home
    Assistant and is naturally sequential, and the proxy already runs each
    connection on its own thread. An async client would add a second concurrency
    model for no benefit.
    """

    def __init__(self):
        self.conn = open_dbus_connection(bus="SYSTEM")

    def _call(self, path, interface, method, signature=None, body=(), timeout=20.0):
        addr = DBusAddress(path, bus_name=BLUEZ, interface=interface)
        msg = new_method_call(addr, method, signature, body)
        reply = self.conn.send_and_get_reply(msg, timeout=timeout)
        if reply.header.message_type is MessageType.error:
            raise BlueZError("%s: %s" % (reply.header.fields.get(4), reply.body))
        return reply.body

    def objects(self):
        addr = DBusAddress("/", bus_name=BLUEZ, interface=I_OM)
        reply = self.conn.send_and_get_reply(
            new_method_call(addr, "GetManagedObjects"), timeout=20.0)
        if reply.header.message_type is MessageType.error:
            raise BlueZError("GetManagedObjects failed: %s" % (reply.body,))
        return reply.body[0]

    def device_path(self, mac):
        return "%s/dev_%s" % (ADAPTER, mac.upper().replace(":", "_"))

    def prop(self, path, interface, name, default=None):
        try:
            body = self._call(path, I_PROPS, "Get", "ss", (interface, name))
        except BlueZError:
            return default
        return body[0][1] if body else default

    # ------------------------------------------------------------ connection

    def connect(self, mac, timeout=25.0):
        """Connect and wait for GATT discovery to finish.

        ServicesResolved is the flag that matters, not Connected: a link can be
        up while the attribute table is still unknown, and answering Home
        Assistant at that point produces an empty service list. This is the same
        distinction that made the first manual test look like a hardware
        failure - the link came up, the discovery never finished, and BlueZ
        aborted with le-connection-abort-by-local.
        """
        path = self.device_path(mac)

        # STOP DISCOVERY FIRST. A connection attempt while the adapter is
        # scanning is refused by this controller with
        # org.bluez.Error.Failed: le-connection-abort-by-local - reproduced
        # every time, and it succeeds every time once discovery is off. The
        # radio is shared and it will not do both.
        #
        # This is the same collision that makes the passive scanner fight any
        # other scan, and it is why an active proxy cannot simply scan
        # continuously alongside its connections.
        self.stop_discovery()
        time.sleep(1.5)

        try:
            self._call(path, I_DEVICE, "Connect", timeout=timeout)
        except BlueZError as err:
            # One retry: an in-flight discovery teardown can still clip the
            # first attempt even after StopDiscovery returns.
            if "abort-by-local" not in str(err):
                raise
            _LOGGER.info("%s: connect aborted locally, retrying once", mac)
            time.sleep(2.0)
            self._call(path, I_DEVICE, "Connect", timeout=timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.prop(path, I_DEVICE, "ServicesResolved", False):
                return True
            time.sleep(0.25)
        _LOGGER.warning("%s connected but services never resolved", mac)
        return False

    def stop_discovery(self):
        """Best-effort: not scanning is not an error."""
        try:
            self._call(ADAPTER, "org.bluez.Adapter1", "StopDiscovery", timeout=8.0)
        except BlueZError:
            pass

    def disconnect(self, mac):
        try:
            self._call(self.device_path(mac), I_DEVICE, "Disconnect")
        except BlueZError as err:
            _LOGGER.debug("disconnect %s: %s", mac, err)

    def is_connected(self, mac):
        return bool(self.prop(self.device_path(mac), I_DEVICE, "Connected", False))

    def mtu(self, mac):
        """Largest MTU any characteristic reports, or the BLE default of 23."""
        best = 23
        prefix = self.device_path(mac) + "/"
        for path, ifaces in self.objects().items():
            if path.startswith(prefix) and I_CHAR in ifaces:
                v = ifaces[I_CHAR].get("MTU")
                if isinstance(v, tuple):
                    v = v[1]
                if isinstance(v, int):
                    best = max(best, v)
        return best

    # ----------------------------------------------------------------- GATT

    def services(self, mac):
        """The attribute tree, shaped the way the ESPHome API wants it."""
        prefix = self.device_path(mac) + "/"
        objs = self.objects()

        def val(d, key, default=None):
            v = d.get(key, default)
            return v[1] if isinstance(v, tuple) else v

        chars = {}
        for path, ifaces in objs.items():
            if not path.startswith(prefix) or I_CHAR not in ifaces:
                continue
            props = ifaces[I_CHAR]
            chars.setdefault(val(props, "Service"), []).append({
                "path": path,
                "uuid": val(props, "UUID", ""),
                "handle": handle_of(path),
                "flags": val(props, "Flags", []) or [],
                "descriptors": [],
            })

        for path, ifaces in objs.items():
            if not path.startswith(prefix) or I_DESC not in ifaces:
                continue
            props = ifaces[I_DESC]
            parent = val(props, "Characteristic")
            for lst in chars.values():
                for c in lst:
                    if c["path"] == parent:
                        c["descriptors"].append(
                            {"uuid": val(props, "UUID", ""), "handle": handle_of(path)})

        out = []
        for path, ifaces in sorted(objs.items()):
            if not path.startswith(prefix) or I_SERVICE not in ifaces:
                continue
            props = ifaces[I_SERVICE]
            out.append({
                "path": path,
                "uuid": val(props, "UUID", ""),
                "handle": handle_of(path),
                "characteristics": sorted(chars.get(path, []),
                                          key=lambda c: c["handle"]),
            })
        return out

    def char_path(self, mac, handle):
        prefix = self.device_path(mac) + "/"
        for path, ifaces in self.objects().items():
            if path.startswith(prefix) and I_CHAR in ifaces and handle_of(path) == handle:
                return path
        return None

    def read(self, path):
        body = self._call(path, I_CHAR, "ReadValue", "a{sv}", ({},))
        return bytes(body[0]) if body else b""

    def write(self, path, data, with_response=True):
        opts = {"type": ("s", "request" if with_response else "command")}
        self._call(path, I_CHAR, "WriteValue", "aya{sv}", (bytes(data), opts))

    def start_notify(self, path):
        self._call(path, I_CHAR, "StartNotify")

    def stop_notify(self, path):
        try:
            self._call(path, I_CHAR, "StopNotify")
        except BlueZError as err:
            _LOGGER.debug("StopNotify %s: %s", path, err)


class NotifyListener(threading.Thread):
    """Deliver GATT notifications.

    BlueZ does not push notification payloads through a method return - it
    updates the characteristic's Value property and emits PropertiesChanged.
    So this needs its OWN D-Bus connection: the blocking client above is busy
    waiting for method replies, and a signal arriving mid-call would be read as
    the reply to whatever was in flight.

    The match rule is narrowed to GattCharacteristic1 so the bus does not wake
    this thread for every property change in BlueZ - on a device that is also
    scanning, that is a great many.
    """

    daemon = True

    def __init__(self, on_value):
        super().__init__(name="gatt-notify")
        self.on_value = on_value
        self._stop = threading.Event()
        self.conn = open_dbus_connection(bus="SYSTEM")
        bus = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus",
                          interface="org.freedesktop.DBus")
        rule = ("type='signal',interface='org.freedesktop.DBus.Properties',"
                "member='PropertiesChanged',arg0='%s'" % I_CHAR)
        self.conn.send_and_get_reply(new_method_call(bus, "AddMatch", "s", (rule,)))

    def stop(self):
        self._stop.set()
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    def run(self):
        while not self._stop.is_set():
            try:
                msg = self.conn.receive()
            except Exception:  # noqa: BLE001
                return
            if msg.header.message_type is not MessageType.signal:
                continue
            if msg.header.fields.get(3) != "PropertiesChanged":
                continue
            try:
                iface, changed, _inv = msg.body
            except (ValueError, TypeError):
                continue
            if iface != I_CHAR or "Value" not in changed:
                continue
            path = msg.header.fields.get(1)
            val = changed["Value"]
            if isinstance(val, tuple):
                val = val[1]
            try:
                self.on_value(path, bytes(val))
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("notification handler failed: %s", err)
