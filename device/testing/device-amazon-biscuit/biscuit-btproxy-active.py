"""Active half of the Bluetooth proxy: the ESPHome GATT message set.

Installed as /usr/lib/biscuit-btproxy/active.py.

Passive forwarding is one message type. Active is twelve, and stateful: Home
Assistant opens connections THROUGH this device, walks the attribute table,
reads, writes and subscribes. This module maps that protocol onto the BlueZ
client in bluez.py.

TWO THINGS THAT SHAPE THE DESIGN

Scanning and connecting cannot overlap. A connection attempt made while the
adapter is discovering fails every time with le-connection-abort-by-local, and
succeeds every time once discovery is stopped - the radio is shared and will
not do both. So the scanner is suspended while any connection is open, and
resumes when the last one closes. This is not a tuning choice; it is the
difference between connections working and not.

The connection limit is a guess, and is written as one. This is a BT 4.0
controller with no debugfs node exposing its LE parameters, so the real
simultaneous-connection ceiling is unmeasured. Home Assistant asks for free
slots and schedules against the answer, so claiming a large number would make
it queue work this radio cannot do. Three is conservative; raise it when it has
actually been measured, not before.
"""

import logging
import threading

import bluez

_LOGGER = logging.getLogger("biscuit-btproxy.active")

# Verified against the generated descriptor rather than transcribed: a wrong id
# does not fail loudly, it produces a message Home Assistant silently ignores.
MT_BLUETOOTH_DEVICE_REQUEST = 68
MT_BLUETOOTH_DEVICE_CONNECTION_RESPONSE = 69
MT_GATT_GET_SERVICES_REQUEST = 70
MT_GATT_GET_SERVICES_RESPONSE = 71
MT_GATT_GET_SERVICES_DONE_RESPONSE = 72
MT_GATT_READ_REQUEST = 73
MT_GATT_READ_RESPONSE = 74
MT_GATT_WRITE_REQUEST = 75
MT_GATT_READ_DESCRIPTOR_REQUEST = 76
MT_GATT_WRITE_DESCRIPTOR_REQUEST = 77
MT_GATT_NOTIFY_REQUEST = 78
MT_GATT_NOTIFY_DATA_RESPONSE = 79
MT_SUBSCRIBE_CONNECTIONS_FREE_REQUEST = 80
MT_CONNECTIONS_FREE_RESPONSE = 81
MT_GATT_ERROR_RESPONSE = 82
MT_GATT_WRITE_RESPONSE = 83
MT_GATT_NOTIFY_RESPONSE = 84
MT_BLUETOOTH_DEVICE_PAIRING_RESPONSE = 85
MT_BLUETOOTH_DEVICE_UNPAIRING_RESPONSE = 86
MT_BLUETOOTH_DEVICE_CLEAR_CACHE_RESPONSE = 88

# BluetoothDeviceRequestType
REQ_CONNECT = 0
REQ_DISCONNECT = 1
REQ_PAIR = 2
REQ_UNPAIR = 3
REQ_CONNECT_V3_WITH_CACHE = 4
REQ_CONNECT_V3_WITHOUT_CACHE = 5
REQ_CLEAR_CACHE = 6

DEFAULT_LIMIT = 3


class ActiveProxy:
    """Connection state shared by every Home Assistant client."""

    def __init__(self, pb, limit=DEFAULT_LIMIT, on_scan_pause=None):
        self.pb = pb
        self.limit = limit
        self.on_scan_pause = on_scan_pause
        self.bz = None
        self.notifier = None
        self.lock = threading.RLock()
        self.connected = {}          # mac -> {"conn": ProxyConnection}
        self.notify_paths = {}       # dbus path -> (mac, handle)
        self.free_subscribers = []

    # ------------------------------------------------------------- lifecycle

    def ensure(self):
        """Open D-Bus lazily.

        Not at startup: a proxy that nobody has asked to connect anything
        should not hold a bus connection or a notification match rule, and on a
        device where Bluetooth deliberately arrives ~40 s after boot, starting
        eagerly would just fail.
        """
        with self.lock:
            if self.bz is None:
                self.bz = bluez.BlueZ()
            if self.notifier is None:
                self.notifier = bluez.NotifyListener(self._on_notify)
                self.notifier.start()
            return self.bz

    def shutdown(self):
        with self.lock:
            for mac in list(self.connected):
                try:
                    self.bz.disconnect(mac)
                except Exception:  # noqa: BLE001
                    pass
            self.connected.clear()
            if self.notifier is not None:
                self.notifier.stop()
                self.notifier = None

    def _scanning_allowed(self):
        """Scanning is only safe when nothing is connected. See the module docstring."""
        return not self.connected

    def _sync_scan(self):
        if self.on_scan_pause is not None:
            self.on_scan_pause(not self._scanning_allowed())

    # ---------------------------------------------------------------- errors

    def _error(self, conn, address, handle, err):
        """Answer with GATTErrorResponse rather than silence.

        Home Assistant waits on a reply for every GATT request; dropping one
        leaves the operation hanging until its own timeout, which looks like a
        slow proxy rather than a failed read.
        """
        # repr, not str: a BlueZ D-Bus error can carry an empty message, and
        # "gatt error handle=41:" with nothing after it says less than nothing.
        _LOGGER.warning("gatt error addr=%012x handle=%d: %r", address, handle, err)
        msg = self.pb.BluetoothGATTErrorResponse()
        msg.address = address
        msg.handle = handle
        msg.error = 133          # ESP_GATT_ERROR, the generic failure HA expects
        conn.send(MT_GATT_ERROR_RESPONSE, msg)

    # ------------------------------------------------------------- responses

    def _connection_response(self, conn, address, connected, mtu=0, error=0):
        msg = self.pb.BluetoothDeviceConnectionResponse()
        msg.address = address
        msg.connected = connected
        msg.mtu = mtu
        msg.error = error
        conn.send(MT_BLUETOOTH_DEVICE_CONNECTION_RESPONSE, msg)

    def broadcast_free(self):
        msg = self.pb.BluetoothConnectionsFreeResponse()
        with self.lock:
            msg.free = max(0, self.limit - len(self.connected))
            msg.limit = self.limit
            targets = list(self.free_subscribers)
        for c in targets:
            try:
                c.send(MT_CONNECTIONS_FREE_RESPONSE, msg)
            except OSError:
                pass

    # -------------------------------------------------------------- notifies

    def _on_notify(self, path, value):
        with self.lock:
            entry = self.notify_paths.get(path)
            targets = [d["conn"] for d in self.connected.values()]
        if not entry:
            return
        mac, handle = entry
        msg = self.pb.BluetoothGATTNotifyDataResponse()
        msg.address = bluez.addr_to_int(mac)
        msg.handle = handle
        msg.data = value
        for c in targets:
            try:
                c.send(MT_GATT_NOTIFY_DATA_RESPONSE, msg)
            except OSError:
                pass

    # --------------------------------------------------------------- handler

    def handle(self, conn, msg_type, payload):
        """True if this module owned the message."""
        if msg_type == MT_SUBSCRIBE_CONNECTIONS_FREE_REQUEST:
            with self.lock:
                if conn not in self.free_subscribers:
                    self.free_subscribers.append(conn)
            self.broadcast_free()
            return True

        if msg_type == MT_BLUETOOTH_DEVICE_REQUEST:
            self._device_request(conn, payload)
            return True

        if msg_type == MT_GATT_GET_SERVICES_REQUEST:
            self._get_services(conn, payload)
            return True

        if msg_type == MT_GATT_READ_REQUEST:
            self._read(conn, payload)
            return True

        if msg_type == MT_GATT_WRITE_REQUEST:
            self._write(conn, payload)
            return True

        if msg_type == MT_GATT_NOTIFY_REQUEST:
            self._notify(conn, payload)
            return True

        if msg_type in (MT_GATT_READ_DESCRIPTOR_REQUEST,
                        MT_GATT_WRITE_DESCRIPTOR_REQUEST):
            # Descriptors are enumerated and advertised, but read/write of them
            # is not implemented. Answering with an error is honest; silence
            # would leave Home Assistant waiting for a reply that never comes.
            req = (self.pb.BluetoothGATTReadDescriptorRequest()
                   if msg_type == MT_GATT_READ_DESCRIPTOR_REQUEST
                   else self.pb.BluetoothGATTWriteDescriptorRequest())
            req.ParseFromString(payload)
            self._error(conn, req.address, req.handle, "descriptor access not implemented")
            return True

        return False

    # ------------------------------------------------------------- internals

    def _device_request(self, conn, payload):
        req = self.pb.BluetoothDeviceRequest()
        req.ParseFromString(payload)
        mac = bluez.int_to_addr(req.address)
        rtype = req.request_type

        if rtype in (REQ_DISCONNECT,):
            with self.lock:
                self.connected.pop(mac, None)
            try:
                self.ensure().disconnect(mac)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("disconnect %s: %s", mac, err)
            self._connection_response(conn, req.address, False)
            self._sync_scan()
            self.broadcast_free()
            return

        if rtype in (REQ_PAIR, REQ_UNPAIR, REQ_CLEAR_CACHE):
            # Deliberately not implemented. Pairing needs an agent and a way for
            # a user to confirm, and this device has no display - the pairing it
            # does have is a bounded window driven from :8080 or a button, not
            # something Home Assistant should trigger silently.
            mt = {REQ_PAIR: MT_BLUETOOTH_DEVICE_PAIRING_RESPONSE,
                  REQ_UNPAIR: MT_BLUETOOTH_DEVICE_UNPAIRING_RESPONSE,
                  REQ_CLEAR_CACHE: MT_BLUETOOTH_DEVICE_CLEAR_CACHE_RESPONSE}[rtype]
            cls = {REQ_PAIR: self.pb.BluetoothDevicePairingResponse,
                   REQ_UNPAIR: self.pb.BluetoothDeviceUnpairingResponse,
                   REQ_CLEAR_CACHE: self.pb.BluetoothDeviceClearCacheResponse}[rtype]
            msg = cls()
            msg.address = req.address
            msg.success = False
            msg.error = 133
            conn.send(mt, msg)
            return

        # Anything else is a connect.
        with self.lock:
            if mac not in self.connected and len(self.connected) >= self.limit:
                _LOGGER.warning("refusing %s: %d/%d connections in use",
                                mac, len(self.connected), self.limit)
                self._connection_response(conn, req.address, False, error=133)
                return

        # Suspend scanning BEFORE connecting, not after: the two cannot overlap.
        self._sync_scan_force_pause()
        try:
            bz = self.ensure()
            resolved = bz.connect(mac, timeout=30.0)
            mtu = bz.mtu(mac) if resolved else 23
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("connect %s failed: %s", mac, err)
            self._connection_response(conn, req.address, False, error=133)
            self._sync_scan()
            return

        with self.lock:
            self.connected[mac] = {"conn": conn}
        _LOGGER.info("connected %s (mtu %d, %d/%d slots)",
                     mac, mtu, len(self.connected), self.limit)
        self._connection_response(conn, req.address, True, mtu=mtu)
        self.broadcast_free()

    def _sync_scan_force_pause(self):
        if self.on_scan_pause is not None:
            self.on_scan_pause(True)

    def _get_services(self, conn, payload):
        req = self.pb.BluetoothGATTGetServicesRequest()
        req.ParseFromString(payload)
        mac = bluez.int_to_addr(req.address)
        try:
            services = self.ensure().services(mac)
        except Exception as err:  # noqa: BLE001
            self._error(conn, req.address, 0, err)
            return

        # One service per message. Home Assistant accepts a repeated field, but
        # the attribute table of a phone runs to tens of kilobytes once every
        # characteristic and descriptor is included, and a single frame that
        # large is a poor thing to put through a 517-byte-MTU link's worth of
        # buffers on a device with this little memory.
        for svc in services:
            msg = self.pb.BluetoothGATTGetServicesResponse()
            msg.address = req.address
            out = msg.services.add()
            out.uuid.extend(bluez.uuid_to_pair(svc["uuid"]))
            out.handle = svc["handle"]
            for ch in svc["characteristics"]:
                c = out.characteristics.add()
                c.uuid.extend(bluez.uuid_to_pair(ch["uuid"]))
                c.handle = ch["handle"]
                c.properties = _props_bitmap(ch["flags"])
                for de in ch["descriptors"]:
                    d = c.descriptors.add()
                    d.uuid.extend(bluez.uuid_to_pair(de["uuid"]))
                    d.handle = de["handle"]
            conn.send(MT_GATT_GET_SERVICES_RESPONSE, msg)

        done = self.pb.BluetoothGATTGetServicesDoneResponse()
        done.address = req.address
        conn.send(MT_GATT_GET_SERVICES_DONE_RESPONSE, done)

    def _read(self, conn, payload):
        req = self.pb.BluetoothGATTReadRequest()
        req.ParseFromString(payload)
        mac = bluez.int_to_addr(req.address)
        try:
            bz = self.ensure()
            path = bz.char_path(mac, req.handle)
            if path is None:
                raise bluez.BlueZError("no characteristic with handle %d" % req.handle)
            data = bz.read(path)
        except Exception as err:  # noqa: BLE001
            self._error(conn, req.address, req.handle, err)
            return
        msg = self.pb.BluetoothGATTReadResponse()
        msg.address = req.address
        msg.handle = req.handle
        msg.data = data
        conn.send(MT_GATT_READ_RESPONSE, msg)

    def _write(self, conn, payload):
        req = self.pb.BluetoothGATTWriteRequest()
        req.ParseFromString(payload)
        mac = bluez.int_to_addr(req.address)
        try:
            bz = self.ensure()
            path = bz.char_path(mac, req.handle)
            if path is None:
                raise bluez.BlueZError("no characteristic with handle %d" % req.handle)
            bz.write(path, req.data, with_response=req.response)
        except Exception as err:  # noqa: BLE001
            self._error(conn, req.address, req.handle, err)
            return
        msg = self.pb.BluetoothGATTWriteResponse()
        msg.address = req.address
        msg.handle = req.handle
        conn.send(MT_GATT_WRITE_RESPONSE, msg)

    def _notify(self, conn, payload):
        req = self.pb.BluetoothGATTNotifyRequest()
        req.ParseFromString(payload)
        mac = bluez.int_to_addr(req.address)
        try:
            bz = self.ensure()
            path = bz.char_path(mac, req.handle)
            if path is None:
                raise bluez.BlueZError("no characteristic with handle %d" % req.handle)
            if req.enable:
                bz.start_notify(path)
                with self.lock:
                    self.notify_paths[path] = (mac, req.handle)
            else:
                bz.stop_notify(path)
                with self.lock:
                    self.notify_paths.pop(path, None)
        except Exception as err:  # noqa: BLE001
            self._error(conn, req.address, req.handle, err)
            return
        msg = self.pb.BluetoothGATTNotifyResponse()
        msg.address = req.address
        msg.handle = req.handle
        conn.send(MT_GATT_NOTIFY_RESPONSE, msg)

    def client_gone(self, conn):
        """Drop a client's connections when it disappears.

        Without this a Home Assistant restart would leave the radio holding
        links nobody is listening to, and the slot count would never recover.
        """
        with self.lock:
            if conn in self.free_subscribers:
                self.free_subscribers.remove(conn)
            macs = [m for m, d in self.connected.items() if d["conn"] is conn]
            for m in macs:
                self.connected.pop(m, None)
        for m in macs:
            try:
                self.bz.disconnect(m)
            except Exception:  # noqa: BLE001
                pass
        if macs:
            _LOGGER.info("client gone; dropped %d connection(s)", len(macs))
        self._sync_scan()
        self.broadcast_free()


def _props_bitmap(flags):
    """BlueZ flag strings to the GATT characteristic property bitmap."""
    bits = {"broadcast": 0x01, "read": 0x02, "write-without-response": 0x04,
            "write": 0x08, "notify": 0x10, "indicate": 0x20,
            "authenticated-signed-writes": 0x40, "extended-properties": 0x80}
    out = 0
    for f in flags:
        out |= bits.get(str(f).lower(), 0)
    return out
